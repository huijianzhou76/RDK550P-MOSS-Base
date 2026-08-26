#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>

#include "cJSON.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "ir_service.h"
#include "motion_controller.h"
#include "peripherals.h"

static const char *TAG = "MOSS_MCU";
static constexpr uart_port_t LINK_UART = UART_NUM_1;
static constexpr int LINK_BAUD = 115200;
static constexpr size_t RX_LINE_MAX = 1536;
static constexpr const char *FIRMWARE_VERSION = "0.2.0";
static constexpr const char *PROTOCOL_VERSION = "1.1";

struct MossState {
    bool estop = false;
    bool link_degraded = false;
    bool watchdog_stopped = false;
    std::string estop_source = "none";
    int64_t last_heartbeat_ms = 0;
};

static MossState g_state;
static MotionController g_motion;
static PeripheralHub g_peripherals;
static IrService g_ir;
static SemaphoreHandle_t g_uart_write_mutex = nullptr;

static int64_t now_ms() { return esp_timer_get_time() / 1000; }

static void set_servo_power(bool enabled) {
    gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_SERVO_POWER_EN_GPIO), enabled ? 1 : 0);
}

static bool servo_power_enabled() {
    return gpio_get_level(static_cast<gpio_num_t>(CONFIG_MOSS_SERVO_POWER_EN_GPIO)) != 0;
}

static void write_json(cJSON *root) {
    if (!root) return;
    char *text = cJSON_PrintUnformatted(root);
    if (!text) return;
    if (g_uart_write_mutex) xSemaphoreTake(g_uart_write_mutex, portMAX_DELAY);
    uart_write_bytes(LINK_UART, text, strlen(text));
    uart_write_bytes(LINK_UART, "\n", 1);
    if (g_uart_write_mutex) xSemaphoreGive(g_uart_write_mutex);
    cJSON_free(text);
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

static void send_event(const char *event_name, const char *source = nullptr) {
    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "v", 1);
    cJSON_AddStringToObject(root, "type", "event");
    cJSON_AddStringToObject(root, "event", event_name);
    cJSON_AddNumberToObject(root, "ts", static_cast<double>(now_ms()));
    cJSON *data = cJSON_AddObjectToObject(root, "data");
    if (source) cJSON_AddStringToObject(data, "source", source);
    cJSON_AddBoolToObject(data, "estop", g_state.estop);
    cJSON_AddBoolToObject(data, "link_degraded", g_state.link_degraded);
    cJSON_AddBoolToObject(data, "servo_power", servo_power_enabled());
    write_json(root);
    cJSON_Delete(root);
}

static void send_error(const char *id, const char *error, const char *message) {
    cJSON *root = response_base(id, false);
    cJSON_AddStringToObject(root, "error", error);
    cJSON_AddStringToObject(root, "message", message);
    write_json(root);
    cJSON_Delete(root);
}

static void add_state(cJSON *data) {
    const HeadPose pose = g_motion.pose();
    const PeripheralStatus peripherals = g_peripherals.status();
    cJSON_AddBoolToObject(data, "estop", g_state.estop);
    cJSON_AddStringToObject(data, "estop_source", g_state.estop_source.c_str());
    cJSON_AddBoolToObject(data, "physical_estop_pressed",
                          gpio_get_level(static_cast<gpio_num_t>(CONFIG_MOSS_ESTOP_GPIO)) == 0);
    cJSON_AddBoolToObject(data, "link_degraded", g_state.link_degraded);
    cJSON_AddBoolToObject(data, "watchdog_stopped", g_state.watchdog_stopped);
    cJSON_AddBoolToObject(data, "motion_enabled", g_motion.enabled());
    cJSON_AddBoolToObject(data, "servo_power_enabled", servo_power_enabled());
    cJSON_AddNumberToObject(data, "yaw_deg", pose.yaw_deg);
    cJSON_AddNumberToObject(data, "pitch_deg", pose.pitch_deg);
    cJSON_AddNumberToObject(data, "last_heartbeat_ms", static_cast<double>(g_state.last_heartbeat_ms));
    cJSON *p = cJSON_AddObjectToObject(data, "peripherals");
    cJSON_AddBoolToObject(p, "oled", peripherals.oled);
    cJSON_AddBoolToObject(p, "ina219", peripherals.ina219);
    cJSON_AddBoolToObject(p, "aht20", peripherals.aht20);
    cJSON_AddBoolToObject(p, "eye_light", peripherals.eye_light);
    cJSON_AddBoolToObject(p, "ir", g_ir.available());
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
static int json_int(cJSON *object, const char *name, int fallback = 0) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsNumber(item) ? item->valueint : fallback;
}
static std::string json_string(cJSON *object, const char *name, const char *fallback = "") {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsString(item) && item->valuestring ? item->valuestring : fallback;
}

static void set_status_led(bool on) {
    gpio_set_level(static_cast<gpio_num_t>(CONFIG_MOSS_STATUS_LED_GPIO), on ? 1 : 0);
}

static void trigger_estop(const char *source) {
    if (g_state.estop) return;
    g_state.estop = true;
    g_state.estop_source = source ? source : "unknown";
    g_motion.emergency_stop();
    set_servo_power(false);
    g_peripherals.set_eye(1.0f);
    set_status_led(true);
    ESP_LOGW(TAG, "E-STOP latched source=%s", g_state.estop_source.c_str());
    send_event("safety.estop", g_state.estop_source.c_str());
}

static void restore_motion_power() {
    if (g_state.estop || g_state.watchdog_stopped) return;
    set_servo_power(true);
    vTaskDelay(pdMS_TO_TICKS(200));
    g_motion.resume();
}

static void touch_heartbeat() {
    g_state.last_heartbeat_ms = now_ms();
    if (g_state.link_degraded || g_state.watchdog_stopped) {
        g_state.link_degraded = false;
        if (g_state.watchdog_stopped && !g_state.estop) {
            g_state.watchdog_stopped = false;
            restore_motion_power();
            g_peripherals.set_eye(0.35f);
            set_status_led(false);
            send_event("safety.link_recovered", "rdk");
        }
    }
}

static bool motion_allowed(const char *id) {
    if (g_state.estop) {
        send_error(id, "ESTOP_ACTIVE", "motion rejected while emergency stop is active");
        return false;
    }
    if (g_state.link_degraded || g_state.watchdog_stopped || !servo_power_enabled()) {
        send_error(id, "LINK_DEGRADED", "motion rejected because motion power is not in a safe enabled state");
        return false;
    }
    return true;
}

static void add_capabilities(cJSON *data) {
    cJSON_AddStringToObject(data, "board", "moss-mcu-esp32s3");
    cJSON_AddStringToObject(data, "firmware", FIRMWARE_VERSION);
    cJSON_AddStringToObject(data, "protocol", PROTOCOL_VERSION);
    cJSON *caps = cJSON_AddArrayToObject(data, "capabilities");
    const char *names[] = {
        "head.queued_motion", "head.cosine_easing", "safety.physical_estop",
        "safety.servo_power_cut", "safety.link_watchdog", "light.red_eye_pwm",
        "display.ssd1306", "sensor.ina219", "sensor.aht20",
        "ir.raw_learn", "ir.raw_replay"
    };
    for (const char *name : names) cJSON_AddItemToArray(caps, cJSON_CreateString(name));
    cJSON *limits = cJSON_AddObjectToObject(data, "limits");
    cJSON_AddNumberToObject(limits, "yaw_min_deg", CONFIG_MOSS_YAW_MIN_DEG);
    cJSON_AddNumberToObject(limits, "yaw_max_deg", CONFIG_MOSS_YAW_MAX_DEG);
    cJSON_AddNumberToObject(limits, "pitch_min_deg", CONFIG_MOSS_PITCH_MIN_DEG);
    cJSON_AddNumberToObject(limits, "pitch_max_deg", CONFIG_MOSS_PITCH_MAX_DEG);
    cJSON_AddNumberToObject(limits, "speed_min", 0.05);
    cJSON_AddNumberToObject(limits, "speed_max", 1.0);
    cJSON_AddNumberToObject(limits, "watchdog_warn_ms", CONFIG_MOSS_WATCHDOG_WARN_MS);
    cJSON_AddNumberToObject(limits, "watchdog_stop_ms", CONFIG_MOSS_WATCHDOG_ESTOP_MS);
    cJSON_AddNumberToObject(limits, "ir_slots", 8);
    cJSON *pins = cJSON_AddObjectToObject(data, "pins");
    cJSON_AddNumberToObject(pins, "yaw_servo", CONFIG_MOSS_YAW_SERVO_GPIO);
    cJSON_AddNumberToObject(pins, "pitch_servo", CONFIG_MOSS_PITCH_SERVO_GPIO);
    cJSON_AddNumberToObject(pins, "servo_power_en", CONFIG_MOSS_SERVO_POWER_EN_GPIO);
    cJSON_AddNumberToObject(pins, "physical_estop", CONFIG_MOSS_ESTOP_GPIO);
    cJSON_AddNumberToObject(pins, "eye_pwm", CONFIG_MOSS_EYE_PWM_GPIO);
    cJSON_AddNumberToObject(pins, "i2c_sda", CONFIG_MOSS_I2C_SDA_GPIO);
    cJSON_AddNumberToObject(pins, "i2c_scl", CONFIG_MOSS_I2C_SCL_GPIO);
    cJSON_AddNumberToObject(pins, "ir_rx", CONFIG_MOSS_IR_RX_GPIO);
    cJSON_AddNumberToObject(pins, "ir_tx", CONFIG_MOSS_IR_TX_GPIO);
}

static void handle_command(cJSON *root) {
    cJSON *id_item = cJSON_GetObjectItemCaseSensitive(root, "id");
    cJSON *action_item = cJSON_GetObjectItemCaseSensitive(root, "action");
    const char *id = cJSON_IsString(id_item) ? id_item->valuestring : "";
    const char *action = cJSON_IsString(action_item) ? action_item->valuestring : nullptr;
    cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
    if (!cJSON_IsObject(params)) params = root;
    if (!action) { send_error(id, "BAD_COMMAND", "missing action"); return; }
    touch_heartbeat();

    if (strcmp(action, "system.heartbeat") == 0) { send_ok_with_state(id); return; }
    if (strcmp(action, "system.ping") == 0) {
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddStringToObject(data, "board", "moss-mcu-esp32s3");
        cJSON_AddStringToObject(data, "firmware", FIRMWARE_VERSION);
        cJSON_AddStringToObject(data, "protocol", PROTOCOL_VERSION);
        cJSON_AddNumberToObject(data, "uptime_ms", static_cast<double>(now_ms()));
        add_state(data);
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "system.capabilities") == 0) {
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        add_capabilities(data);
        add_state(data);
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "system.status") == 0) { send_ok_with_state(id); return; }
    if (strcmp(action, "system.estop") == 0) { trigger_estop("software"); send_ok_with_state(id); return; }
    if (strcmp(action, "system.estop_clear") == 0) {
        if (!json_bool(params, "operator_confirmed", false)) {
            send_error(id, "CONFIRMATION_REQUIRED", "operator_confirmed=true is required");
            return;
        }
        if (gpio_get_level(static_cast<gpio_num_t>(CONFIG_MOSS_ESTOP_GPIO)) == 0) {
            send_error(id, "PHYSICAL_ESTOP_ACTIVE", "release the physical E-STOP before clearing the latch");
            return;
        }
        g_state.estop = false;
        g_state.estop_source = "none";
        restore_motion_power();
        g_peripherals.set_eye(0.35f);
        set_status_led(false);
        send_event("safety.estop_cleared", "operator");
        send_ok_with_state(id);
        return;
    }
    if (strcmp(action, "head.center") == 0) {
        if (!motion_allowed(id)) return;
        const float speed = json_number(params, "speed", 0.45f);
        if (!g_motion.center(speed)) { send_error(id, "MOTION_QUEUE", "unable to queue center motion"); return; }
        send_ok_with_state(id);
        return;
    }
    if (strcmp(action, "head.move") == 0) {
        if (!motion_allowed(id)) return;
        const HeadPose current = g_motion.pose();
        const float yaw = json_number(params, "yaw_deg", current.yaw_deg);
        const float pitch = json_number(params, "pitch_deg", current.pitch_deg);
        const float speed = json_number(params, "speed", 0.5f);
        if (yaw < CONFIG_MOSS_YAW_MIN_DEG || yaw > CONFIG_MOSS_YAW_MAX_DEG) { send_error(id, "ANGLE_LIMIT", "yaw exceeds configured safe range"); return; }
        if (pitch < CONFIG_MOSS_PITCH_MIN_DEG || pitch > CONFIG_MOSS_PITCH_MAX_DEG) { send_error(id, "ANGLE_LIMIT", "pitch exceeds configured safe range"); return; }
        if (!g_motion.enqueue(yaw, pitch, speed)) { send_error(id, "MOTION_QUEUE", "unable to queue head motion"); return; }
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddBoolToObject(data, "queued", true);
        cJSON_AddNumberToObject(data, "target_yaw_deg", yaw);
        cJSON_AddNumberToObject(data, "target_pitch_deg", pitch);
        cJSON_AddNumberToObject(data, "speed", std::max(0.05f, std::min(1.0f, speed)));
        add_state(data);
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "light.set") == 0) {
        const float brightness = std::max(0.0f, std::min(1.0f, json_number(params, "brightness", 0.0f)));
        g_peripherals.set_eye(brightness);
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddNumberToObject(data, "brightness", brightness);
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "display.text") == 0) {
        const std::string text = json_string(params, "text", "MOSS");
        if (!g_peripherals.status().oled) { send_error(id, "DEVICE_UNAVAILABLE", "SSD1306 OLED not detected on I2C bus"); return; }
        if (!g_peripherals.display_text(text.substr(0, 128))) { send_error(id, "DISPLAY_ERROR", "failed to update OLED"); return; }
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddStringToObject(data, "text", text.substr(0, 128).c_str());
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "sensor.read") == 0) {
        const SensorSnapshot sensors = g_peripherals.read_sensors();
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddBoolToObject(data, "power_valid", sensors.power_valid);
        cJSON_AddNumberToObject(data, "servo_bus_v", sensors.servo_bus_v);
        cJSON_AddNumberToObject(data, "servo_current_ma", sensors.servo_current_ma);
        cJSON_AddBoolToObject(data, "environment_valid", sensors.environment_valid);
        cJSON_AddNumberToObject(data, "temperature_c", sensors.temperature_c);
        cJSON_AddNumberToObject(data, "humidity_percent", sensors.humidity_percent);
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "ir.learn") == 0) {
        if (!g_ir.available()) { send_error(id, "DEVICE_UNAVAILABLE", "IR RMT service is unavailable"); return; }
        const std::string slot = json_string(params, "slot", "default");
        const int timeout_ms = std::max(500, std::min(4500, json_int(params, "timeout_ms", 4000)));
        size_t count = 0;
        if (!g_ir.learn(slot, timeout_ms, &count)) { send_error(id, "IR_LEARN_FAILED", "no valid IR frame captured or slot table full"); return; }
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddStringToObject(data, "slot", slot.substr(0, 16).c_str());
        cJSON_AddNumberToObject(data, "symbols", static_cast<double>(count));
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "ir.send") == 0) {
        if (!g_ir.available()) { send_error(id, "DEVICE_UNAVAILABLE", "IR RMT service is unavailable"); return; }
        const std::string slot = json_string(params, "slot", "default");
        const int repeat = std::max(1, std::min(5, json_int(params, "repeat", 1)));
        if (!g_ir.send(slot, repeat)) { send_error(id, "IR_SEND_FAILED", "slot not learned or transmit failed"); return; }
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON_AddStringToObject(data, "slot", slot.substr(0, 16).c_str());
        cJSON_AddNumberToObject(data, "repeat", repeat);
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    if (strcmp(action, "ir.list") == 0) {
        cJSON *out = response_base(id, true);
        cJSON *data = cJSON_AddObjectToObject(out, "data");
        cJSON *slots = cJSON_AddArrayToObject(data, "slots");
        for (const auto &slot : g_ir.slots()) cJSON_AddItemToArray(slots, cJSON_CreateString(slot.c_str()));
        write_json(out);
        cJSON_Delete(out);
        return;
    }
    send_error(id, "UNKNOWN_ACTION", action);
}

static void serial_task(void *) {
    char line[RX_LINE_MAX];
    size_t used = 0;
    while (true) {
        uint8_t ch = 0;
        const int n = uart_read_bytes(LINK_UART, &ch, 1, pdMS_TO_TICKS(100));
        if (n <= 0) continue;
        if (ch == '\n') {
            if (used == 0) continue;
            line[used] = '\0';
            cJSON *root = cJSON_Parse(line);
            if (root && cJSON_IsObject(root)) {
                cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
                if (cJSON_IsString(type) && strcmp(type->valuestring, "command") == 0) handle_command(root);
            }
            cJSON_Delete(root);
            used = 0;
            continue;
        }
        if (ch == '\r') continue;
        if (used < RX_LINE_MAX - 1) line[used++] = static_cast<char>(ch);
        else used = 0;
    }
}

static void watchdog_task(void *) {
    while (true) {
        const int64_t age = now_ms() - g_state.last_heartbeat_ms;
        if (age > CONFIG_MOSS_WATCHDOG_WARN_MS && !g_state.link_degraded) {
            g_state.link_degraded = true;
            send_event("safety.link_degraded", "watchdog");
        }
        if (age > CONFIG_MOSS_WATCHDOG_ESTOP_MS && !g_state.estop && !g_state.watchdog_stopped) {
            g_state.watchdog_stopped = true;
            g_motion.emergency_stop();
            set_servo_power(false);
            g_peripherals.set_eye(0.8f);
            set_status_led(true);
            send_event("safety.link_lost", "watchdog");
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static void physical_estop_task(void *) {
    int stable_low_count = 0;
    while (true) {
        const bool pressed_or_open_circuit = gpio_get_level(static_cast<gpio_num_t>(CONFIG_MOSS_ESTOP_GPIO)) == 0;
        if (pressed_or_open_circuit) {
            stable_low_count = std::min(stable_low_count + 1, 10);
            if (stable_low_count >= 5 && !g_state.estop) trigger_estop("physical");
        } else {
            stable_low_count = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
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
    ESP_ERROR_CHECK(uart_set_pin(LINK_UART, CONFIG_MOSS_UART_TX_GPIO, CONFIG_MOSS_UART_RX_GPIO,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
}

static void init_status_led() {
    gpio_config_t config = {};
    config.pin_bit_mask = 1ULL << CONFIG_MOSS_STATUS_LED_GPIO;
    config.mode = GPIO_MODE_OUTPUT;
    config.pull_up_en = GPIO_PULLUP_DISABLE;
    config.pull_down_en = GPIO_PULLDOWN_DISABLE;
    config.intr_type = GPIO_INTR_DISABLE;
    ESP_ERROR_CHECK(gpio_config(&config));
    set_status_led(false);
}

static void init_servo_power_output() {
    gpio_config_t config = {};
    config.pin_bit_mask = 1ULL << CONFIG_MOSS_SERVO_POWER_EN_GPIO;
    config.mode = GPIO_MODE_OUTPUT;
    config.pull_up_en = GPIO_PULLUP_DISABLE;
    config.pull_down_en = GPIO_PULLDOWN_ENABLE;
    config.intr_type = GPIO_INTR_DISABLE;
    ESP_ERROR_CHECK(gpio_config(&config));
    set_servo_power(false);
}

static void init_estop_input() {
    // Fail-safe wiring: NC contact connects GPIO to 3.3V while healthy. Pressing
    // the E-STOP, unplugging the cable, or breaking the wire lets pull-down win.
    gpio_config_t config = {};
    config.pin_bit_mask = 1ULL << CONFIG_MOSS_ESTOP_GPIO;
    config.mode = GPIO_MODE_INPUT;
    config.pull_up_en = GPIO_PULLUP_DISABLE;
    config.pull_down_en = GPIO_PULLDOWN_ENABLE;
    config.intr_type = GPIO_INTR_DISABLE;
    ESP_ERROR_CHECK(gpio_config(&config));
}

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "MOSS MCU Hardware V1.1 boot firmware=%s protocol=%s", FIRMWARE_VERSION, PROTOCOL_VERSION);
    g_uart_write_mutex = xSemaphoreCreateMutex();
    init_status_led();
    init_servo_power_output();
    init_estop_input();
    init_uart();

    ESP_ERROR_CHECK(g_motion.init(CONFIG_MOSS_YAW_SERVO_GPIO, CONFIG_MOSS_PITCH_SERVO_GPIO) ? ESP_OK : ESP_FAIL);
    g_peripherals.init(CONFIG_MOSS_I2C_SDA_GPIO, CONFIG_MOSS_I2C_SCL_GPIO, CONFIG_MOSS_EYE_PWM_GPIO);
    if (!g_ir.init(CONFIG_MOSS_IR_RX_GPIO, CONFIG_MOSS_IR_TX_GPIO)) {
        ESP_LOGW(TAG, "IR service unavailable; continuing without IR");
    }

    g_state.last_heartbeat_ms = now_ms();
    if (gpio_get_level(static_cast<gpio_num_t>(CONFIG_MOSS_ESTOP_GPIO)) == 0) {
        trigger_estop("physical_boot");
    } else {
        set_servo_power(true);
        vTaskDelay(pdMS_TO_TICKS(200));
        g_motion.resume();
        g_motion.center(0.35f);
        g_peripherals.set_eye(0.35f);
    }

    xTaskCreate(serial_task, "moss_serial", 8192, nullptr, 6, nullptr);
    xTaskCreate(watchdog_task, "moss_watchdog", 3072, nullptr, 8, nullptr);
    xTaskCreate(physical_estop_task, "moss_estop", 3072, nullptr, 9, nullptr);
    ESP_LOGI(TAG, "MOSS MCU ready");
}
