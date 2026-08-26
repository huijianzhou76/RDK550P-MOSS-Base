#include <cmath>
#include <cstring>
#include <string>

#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "MOSS_MCU";
static constexpr uart_port_t LINK_UART = UART_NUM_1;
static constexpr int LINK_BAUD = 115200;
static constexpr size_t RX_LINE_MAX = 1024;
static constexpr uint32_t SERVO_PERIOD_US = 20000;
static constexpr uint32_t SERVO_CENTER_US = 1500;
static constexpr uint32_t SERVO_RANGE_US = 500;  // -90..+90 => 1000..2000 us

struct MossState {
    bool estop = false;
    bool link_degraded = false;
    bool servos_enabled = true;
    float yaw_deg = 0.0f;
    float pitch_deg = 0.0f;
    int64_t last_heartbeat_ms = 0;
};

static MossState g_state;

static int64_t now_ms() {
    return esp_timer_get_time() / 1000;
}

static uint32_t pulse_to_duty(uint32_t pulse_us) {
    constexpr uint32_t max_duty = (1u << 16) - 1;
    return static_cast<uint32_t>((static_cast<uint64_t>(pulse_us) * max_duty) / SERVO_PERIOD_US);
}

static uint32_t angle_to_pulse(float angle_deg) {
    float clamped = fmaxf(-90.0f, fminf(90.0f, angle_deg));
    return static_cast<uint32_t>(SERVO_CENTER_US + (clamped / 90.0f) * SERVO_RANGE_US);
}

static void set_servo_enabled(bool enabled) {
    g_state.servos_enabled = enabled;
    if (!enabled) {
        ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
        ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, 0);
        return;
    }
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, pulse_to_duty(angle_to_pulse(g_state.yaw_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, pulse_to_duty(angle_to_pulse(g_state.pitch_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
}

static void apply_head(float yaw_deg, float pitch_deg) {
    g_state.yaw_deg = yaw_deg;
    g_state.pitch_deg = pitch_deg;
    if (!g_state.servos_enabled) {
        return;
    }
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, pulse_to_duty(angle_to_pulse(yaw_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, pulse_to_duty(angle_to_pulse(pitch_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
}

static void write_json(cJSON *root) {
    char *text = cJSON_PrintUnformatted(root);
    if (text != nullptr) {
        uart_write_bytes(LINK_UART, text, strlen(text));
        uart_write_bytes(LINK_UART, "\n", 1);
        cJSON_free(text);
    }
}

static cJSON *response_base(const char *id, bool ok) {
    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "v", 1);
    cJSON_AddStringToObject(root, "id", id ? id : "");
    cJSON_AddStringToObject(root, "type", "response");
    cJSON_AddBoolToObject(root, "ok", ok);
    cJSON_AddNumberToObject(root, "ts", static_cast<double>(now_ms()));
    return root;
}

static void send_error(const char *id, const char *error, const char *message) {
    cJSON *root = response_base(id, false);
    cJSON_AddStringToObject(root, "error", error);
    cJSON_AddStringToObject(root, "message", message);
    write_json(root);
    cJSON_Delete(root);
}

static void add_state(cJSON *data) {
    cJSON_AddBoolToObject(data, "estop", g_state.estop);
    cJSON_AddBoolToObject(data, "link_degraded", g_state.link_degraded);
    cJSON_AddBoolToObject(data, "servos_enabled", g_state.servos_enabled);
    cJSON_AddNumberToObject(data, "yaw_deg", g_state.yaw_deg);
    cJSON_AddNumberToObject(data, "pitch_deg", g_state.pitch_deg);
    cJSON_AddNumberToObject(data, "last_heartbeat_ms", static_cast<double>(g_state.last_heartbeat_ms));
}

static void send_ok_with_state(const char *id) {
    cJSON *root = response_base(id, true);
    cJSON *data = cJSON_AddObjectToObject(root, "data");
    add_state(data);
    write_json(root);
    cJSON_Delete(root);
}

static bool json_bool(cJSON *object, const char *name, bool fallback = false) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsBool(item) ? cJSON_IsTrue(item) : fallback;
}

static float json_number(cJSON *object, const char *name, float fallback = 0.0f) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsNumber(item) ? static_cast<float>(item->valuedouble) : fallback;
}

static bool motion_allowed(const char *id) {
    if (g_state.estop) {
        send_error(id, "ESTOP_ACTIVE", "motion rejected while emergency stop is active");
        return false;
    }
    if (g_state.link_degraded) {
        send_error(id, "LINK_DEGRADED", "motion rejected because RDK heartbeat is stale");
        return false;
    }
    return true;
}

static void handle_command(cJSON *root) {
    cJSON *id_item = cJSON_GetObjectItemCaseSensitive(root, "id");
    cJSON *action_item = cJSON_GetObjectItemCaseSensitive(root, "action");
    const char *id = cJSON_IsString(id_item) ? id_item->valuestring : "";
    const char *action = cJSON_IsString(action_item) ? action_item->valuestring : nullptr;
    cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
    if (!cJSON_IsObject(params)) {
        params = root;
    }

    g_state.last_heartbeat_ms = now_ms();
    g_state.link_degraded = false;

    if (action == nullptr) {
        send_error(id, "BAD_COMMAND", "missing action");
        return;
    }

    if (strcmp(action, "system.ping") == 0) {
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddStringToObject(data, "board", "moss-mcu-esp32");
        cJSON_AddStringToObject(data, "firmware", "0.1.0");
        cJSON_AddNumberToObject(data, "uptime_ms", static_cast<double>(now_ms()));
        add_state(data);
        write_json(out);
        cJSON_Delete(out);
        return;
    }

    if (strcmp(action, "system.status") == 0) {
        send_ok_with_state(id);
        return;
    }

    if (strcmp(action, "system.estop") == 0) {
        g_state.estop = true;
        set_servo_enabled(false);
        gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_STATUS_LED_GPIO), 1);
        send_ok_with_state(id);
        return;
    }

    if (strcmp(action, "system.estop_clear") == 0) {
        if (!json_bool(params, "operator_confirmed", false)) {
            send_error(id, "CONFIRMATION_REQUIRED", "operator_confirmed=true is required");
            return;
        }
        g_state.estop = false;
        set_servo_enabled(true);
        gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_STATUS_LED_GPIO), 0);
        send_ok_with_state(id);
        return;
    }

    if (strcmp(action, "head.center") == 0) {
        if (!motion_allowed(id)) return;
        apply_head(0.0f, 0.0f);
        send_ok_with_state(id);
        return;
    }

    if (strcmp(action, "head.move") == 0) {
        if (!motion_allowed(id)) return;
        float yaw = json_number(params, "yaw_deg", g_state.yaw_deg);
        float pitch = json_number(params, "pitch_deg", g_state.pitch_deg);
        if (yaw < CONFIG_MOSS_YAW_MIN_DEG || yaw > CONFIG_MOSS_YAW_MAX_DEG) {
            send_error(id, "ANGLE_LIMIT", "yaw exceeds configured safe range");
            return;
        }
        if (pitch < CONFIG_MOSS_PITCH_MIN_DEG || pitch > CONFIG_MOSS_PITCH_MAX_DEG) {
            send_error(id, "ANGLE_LIMIT", "pitch exceeds configured safe range");
            return;
        }
        apply_head(yaw, pitch);
        send_ok_with_state(id);
        return;
    }

    if (strcmp(action, "light.set") == 0) {
        float brightness = json_number(params, "brightness", 0.0f);
        gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_STATUS_LED_GPIO), brightness > 0.01f ? 1 : 0);
        send_ok_with_state(id);
        return;
    }

    if (strcmp(action, "display.text") == 0 || strcmp(action, "sensor.read") == 0) {
        send_error(id, "NOT_IMPLEMENTED", "action reserved by protocol but not implemented in firmware 0.1.0");
        return;
    }

    send_error(id, "UNKNOWN_ACTION", action);
}

static void serial_task(void *) {
    char line[RX_LINE_MAX];
    size_t used = 0;
    while (true) {
        uint8_t ch = 0;
        int n = uart_read_bytes(LINK_UART, &ch, 1, pdMS_TO_TICKS(100));
        if (n <= 0) continue;
        if (ch == '\n') {
            if (used == 0) continue;
            line[used] = '\0';
            cJSON *root = cJSON_Parse(line);
            if (root && cJSON_IsObject(root)) {
                cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
                if (cJSON_IsString(type) && strcmp(type->valuestring, "command") == 0) {
                    handle_command(root);
                }
            }
            cJSON_Delete(root);
            used = 0;
            continue;
        }
        if (ch == '\r') continue;
        if (used < RX_LINE_MAX - 1) {
            line[used++] = static_cast<char>(ch);
        } else {
            used = 0;
        }
    }
}

static void watchdog_task(void *) {
    while (true) {
        int64_t age = now_ms() - g_state.last_heartbeat_ms;
        if (age > CONFIG_MOSS_WATCHDOG_WARN_MS) {
            g_state.link_degraded = true;
        }
        if (age > CONFIG_MOSS_WATCHDOG_ESTOP_MS && !g_state.estop) {
            // Link loss is a motion-safe state. It does not clear without a new RDK command.
            set_servo_enabled(false);
            gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_STATUS_LED_GPIO), 1);
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static void init_uart() {
    uart_config_t config = {};
    config.baud_rate = LINK_BAUD;
    config.data_bits = UART_DATA_8_BITS;
    config.parity = UART_PARITY_DISABLE;
    config.stop_bits = UART_STOP_BITS_1;
    config.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
    config.source_clk = UART_SCLK_DEFAULT;
    ESP_ERROR_CHECK(uart_driver_install(LINK_UART, 4096, 4096, 0, nullptr, 0));
    ESP_ERROR_CHECK(uart_param_config(LINK_UART, &config));
    ESP_ERROR_CHECK(uart_set_pin(
        LINK_UART,
        CONFIG_MOSS_UART_TX_GPIO,
        CONFIG_MOSS_UART_RX_GPIO,
        UART_PIN_NO_CHANGE,
        UART_PIN_NO_CHANGE));
}

static void init_servos() {
    ledc_timer_config_t timer = {};
    timer.speed_mode = LEDC_LOW_SPEED_MODE;
    timer.duty_resolution = LEDC_TIMER_16_BIT;
    timer.timer_num = LEDC_TIMER_0;
    timer.freq_hz = 50;
    timer.clk_cfg = LEDC_AUTO_CLK;
    ESP_ERROR_CHECK(ledc_timer_config(&timer));

    ledc_channel_config_t yaw = {};
    yaw.gpio_num = CONFIG_MOSS_YAW_SERVO_GPIO;
    yaw.speed_mode = LEDC_LOW_SPEED_MODE;
    yaw.channel = LEDC_CHANNEL_0;
    yaw.intr_type = LEDC_INTR_DISABLE;
    yaw.timer_sel = LEDC_TIMER_0;
    yaw.duty = pulse_to_duty(SERVO_CENTER_US);
    yaw.hpoint = 0;
    ESP_ERROR_CHECK(ledc_channel_config(&yaw));

    ledc_channel_config_t pitch = yaw;
    pitch.gpio_num = CONFIG_MOSS_PITCH_SERVO_GPIO;
    pitch.channel = LEDC_CHANNEL_1;
    ESP_ERROR_CHECK(ledc_channel_config(&pitch));
}

static void init_status_led() {
    gpio_config_t config = {};
    config.pin_bit_mask = 1ULL << CONFIG_MOSS_STATUS_LED_GPIO;
    config.mode = GPIO_MODE_OUTPUT;
    config.pull_up_en = GPIO_PULLUP_DISABLE;
    config.pull_down_en = GPIO_PULLDOWN_DISABLE;
    config.intr_type = GPIO_INTR_DISABLE;
    ESP_ERROR_CHECK(gpio_config(&config));
    gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_STATUS_LED_GPIO), 0);
}

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "MOSS MCU booting");
    init_status_led();
    init_servos();
    init_uart();
    g_state.last_heartbeat_ms = now_ms();
    apply_head(0.0f, 0.0f);

    xTaskCreate(serial_task, "moss_serial", 6144, nullptr, 6, nullptr);
    xTaskCreate(watchdog_task, "moss_watchdog", 3072, nullptr, 8, nullptr);
    ESP_LOGI(TAG, "MOSS MCU ready");
}
