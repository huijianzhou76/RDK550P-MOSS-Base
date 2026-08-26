#include "peripherals.h"

#include <algorithm>
#include <cctype>
#include <cstring>

#include "driver/ledc.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace {
const char *TAG = "MOSS_PERIPHERALS";
constexpr uint8_t OLED_ADDR = 0x3C;
constexpr uint8_t INA219_ADDR = 0x40;
constexpr uint8_t AHT20_ADDR = 0x38;
constexpr uint32_t I2C_SPEED = 400000;
constexpr int IO_TIMEOUT_MS = 100;

static i2c_master_dev_handle_t add_device(i2c_master_bus_handle_t bus, uint8_t address) {
    i2c_device_config_t config = {};
    config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    config.device_address = address;
    config.scl_speed_hz = I2C_SPEED;
    i2c_master_dev_handle_t handle = nullptr;
    if (i2c_master_bus_add_device(bus, &config, &handle) != ESP_OK) return nullptr;
    return handle;
}
}

bool PeripheralHub::init(int sda_gpio, int scl_gpio, int eye_pwm_gpio) {
    status_.eye_light = init_eye(eye_pwm_gpio);
    if (!init_i2c(sda_gpio, scl_gpio)) {
        ESP_LOGW(TAG, "I2C bus unavailable; OLED and sensors disabled");
        return status_.eye_light;
    }
    status_.oled = init_oled();
    status_.ina219 = init_ina219();
    status_.aht20 = init_aht20();
    ESP_LOGI(TAG, "ready oled=%d ina219=%d aht20=%d eye=%d",
             status_.oled, status_.ina219, status_.aht20, status_.eye_light);
    return true;
}

bool PeripheralHub::init_i2c(int sda_gpio, int scl_gpio) {
    i2c_master_bus_config_t config = {};
    config.i2c_port = I2C_NUM_0;
    config.sda_io_num = static_cast<gpio_num_t>(sda_gpio);
    config.scl_io_num = static_cast<gpio_num_t>(scl_gpio);
    config.clk_source = I2C_CLK_SRC_DEFAULT;
    config.glitch_ignore_cnt = 7;
    config.flags.enable_internal_pullup = true;
    return i2c_new_master_bus(&config, &bus_) == ESP_OK;
}

bool PeripheralHub::init_eye(int gpio) {
    ledc_timer_config_t timer = {};
    timer.speed_mode = LEDC_LOW_SPEED_MODE;
    timer.duty_resolution = LEDC_TIMER_12_BIT;
    timer.timer_num = LEDC_TIMER_1;
    timer.freq_hz = 5000;
    timer.clk_cfg = LEDC_AUTO_CLK;
    if (ledc_timer_config(&timer) != ESP_OK) return false;

    ledc_channel_config_t channel = {};
    channel.gpio_num = gpio;
    channel.speed_mode = LEDC_LOW_SPEED_MODE;
    channel.channel = LEDC_CHANNEL_2;
    channel.intr_type = LEDC_INTR_DISABLE;
    channel.timer_sel = LEDC_TIMER_1;
    channel.duty = 0;
    channel.hpoint = 0;
    return ledc_channel_config(&channel) == ESP_OK;
}

void PeripheralHub::set_eye(float brightness) {
    if (!status_.eye_light) return;
    const float value = std::max(0.0f, std::min(1.0f, brightness));
    const uint32_t duty = static_cast<uint32_t>(value * 4095.0f);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_2, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_2);
}

bool PeripheralHub::init_oled() {
    if (i2c_master_probe(bus_, OLED_ADDR, IO_TIMEOUT_MS) != ESP_OK) return false;
    oled_ = add_device(bus_, OLED_ADDR);
    if (!oled_) return false;
    const uint8_t commands[] = {
        0xAE, 0x20, 0x00, 0xB0, 0xC8, 0x00, 0x10, 0x40,
        0x81, 0x7F, 0xA1, 0xA6, 0xA8, 0x3F, 0xA4, 0xD3,
        0x00, 0xD5, 0x80, 0xD9, 0xF1, 0xDA, 0x12, 0xDB,
        0x40, 0x8D, 0x14, 0xAF,
    };
    for (uint8_t command : commands) {
        if (!oled_command(command)) return false;
    }
    oled_clear_buffer();
    return display_text("MOSS\nMCU ONLINE");
}

bool PeripheralHub::oled_command(uint8_t cmd) {
    if (!oled_) return false;
    uint8_t frame[2] = {0x00, cmd};
    return i2c_master_transmit(oled_, frame, sizeof(frame), IO_TIMEOUT_MS) == ESP_OK;
}

bool PeripheralHub::oled_data(const uint8_t *data, size_t len) {
    if (!oled_) return false;
    uint8_t frame[33];
    while (len > 0) {
        const size_t chunk = std::min<size_t>(32, len);
        frame[0] = 0x40;
        memcpy(frame + 1, data, chunk);
        if (i2c_master_transmit(oled_, frame, chunk + 1, IO_TIMEOUT_MS) != ESP_OK) return false;
        data += chunk;
        len -= chunk;
    }
    return true;
}

void PeripheralHub::oled_clear_buffer() {
    memset(oled_buffer_, 0, sizeof(oled_buffer_));
}

const uint8_t *PeripheralHub::glyph_for(char input) const {
    static const uint8_t blank[5] = {0, 0, 0, 0, 0};
    static const uint8_t unknown[5] = {0x02, 0x01, 0x59, 0x09, 0x06};
    static const uint8_t dash[5] = {0x08, 0x08, 0x08, 0x08, 0x08};
    static const uint8_t dot[5] = {0x00, 0x60, 0x60, 0x00, 0x00};
    static const uint8_t colon[5] = {0x00, 0x36, 0x36, 0x00, 0x00};
    static const uint8_t slash[5] = {0x20, 0x10, 0x08, 0x04, 0x02};
    static const uint8_t underscore[5] = {0x40, 0x40, 0x40, 0x40, 0x40};
    static const uint8_t digits[10][5] = {
        {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},
        {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4B,0x31},
        {0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},
        {0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},
        {0x36,0x49,0x49,0x49,0x36},{0x06,0x49,0x49,0x29,0x1E},
    };
    static const uint8_t letters[26][5] = {
        {0x7E,0x11,0x11,0x11,0x7E},{0x7F,0x49,0x49,0x49,0x36},
        {0x3E,0x41,0x41,0x41,0x22},{0x7F,0x41,0x41,0x22,0x1C},
        {0x7F,0x49,0x49,0x49,0x41},{0x7F,0x09,0x09,0x09,0x01},
        {0x3E,0x41,0x49,0x49,0x7A},{0x7F,0x08,0x08,0x08,0x7F},
        {0x00,0x41,0x7F,0x41,0x00},{0x20,0x40,0x41,0x3F,0x01},
        {0x7F,0x08,0x14,0x22,0x41},{0x7F,0x40,0x40,0x40,0x40},
        {0x7F,0x02,0x0C,0x02,0x7F},{0x7F,0x04,0x08,0x10,0x7F},
        {0x3E,0x41,0x41,0x41,0x3E},{0x7F,0x09,0x09,0x09,0x06},
        {0x3E,0x41,0x51,0x21,0x5E},{0x7F,0x09,0x19,0x29,0x46},
        {0x46,0x49,0x49,0x49,0x31},{0x01,0x01,0x7F,0x01,0x01},
        {0x3F,0x40,0x40,0x40,0x3F},{0x1F,0x20,0x40,0x20,0x1F},
        {0x3F,0x40,0x38,0x40,0x3F},{0x63,0x14,0x08,0x14,0x63},
        {0x07,0x08,0x70,0x08,0x07},{0x61,0x51,0x49,0x45,0x43},
    };
    char ch = static_cast<char>(std::toupper(static_cast<unsigned char>(input)));
    if (ch == ' ') return blank;
    if (ch >= '0' && ch <= '9') return digits[ch - '0'];
    if (ch >= 'A' && ch <= 'Z') return letters[ch - 'A'];
    if (ch == '-') return dash;
    if (ch == '.') return dot;
    if (ch == ':') return colon;
    if (ch == '/') return slash;
    if (ch == '_') return underscore;
    return unknown;
}

void PeripheralHub::draw_char(int x, int page, char ch) {
    if (x < 0 || x + 5 >= 128 || page < 0 || page >= 8) return;
    const uint8_t *glyph = glyph_for(ch);
    const size_t offset = static_cast<size_t>(page) * 128 + static_cast<size_t>(x);
    for (int i = 0; i < 5; ++i) oled_buffer_[offset + i] = glyph[i];
    oled_buffer_[offset + 5] = 0;
}

bool PeripheralHub::display_text(const std::string &text) {
    if (!oled_) return false;
    oled_clear_buffer();
    int x = 0;
    int page = 0;
    for (char ch : text) {
        if (ch == '\n') {
            x = 0;
            ++page;
            if (page >= 8) break;
            continue;
        }
        if (x > 122) {
            x = 0;
            ++page;
            if (page >= 8) break;
        }
        draw_char(x, page, ch);
        x += 6;
    }
    if (!oled_command(0x21) || !oled_command(0x00) || !oled_command(0x7F)) return false;
    if (!oled_command(0x22) || !oled_command(0x00) || !oled_command(0x07)) return false;
    return oled_data(oled_buffer_, sizeof(oled_buffer_));
}

bool PeripheralHub::init_ina219() {
    if (i2c_master_probe(bus_, INA219_ADDR, IO_TIMEOUT_MS) != ESP_OK) return false;
    ina219_ = add_device(bus_, INA219_ADDR);
    return ina219_ != nullptr;
}

bool PeripheralHub::ina219_read16(uint8_t reg, uint16_t *value) {
    if (!ina219_ || !value) return false;
    uint8_t data[2]{};
    if (i2c_master_transmit_receive(ina219_, &reg, 1, data, 2, IO_TIMEOUT_MS) != ESP_OK) return false;
    *value = static_cast<uint16_t>((static_cast<uint16_t>(data[0]) << 8) | data[1]);
    return true;
}

bool PeripheralHub::init_aht20() {
    if (i2c_master_probe(bus_, AHT20_ADDR, IO_TIMEOUT_MS) != ESP_OK) return false;
    aht20_ = add_device(bus_, AHT20_ADDR);
    if (!aht20_) return false;
    uint8_t init_cmd[3] = {0xBE, 0x08, 0x00};
    if (i2c_master_transmit(aht20_, init_cmd, sizeof(init_cmd), IO_TIMEOUT_MS) != ESP_OK) return false;
    vTaskDelay(pdMS_TO_TICKS(20));
    return true;
}

SensorSnapshot PeripheralHub::read_sensors() {
    SensorSnapshot result{};
    if (ina219_) {
        uint16_t bus_raw = 0;
        uint16_t shunt_raw_u = 0;
        if (ina219_read16(0x02, &bus_raw) && ina219_read16(0x01, &shunt_raw_u)) {
            const int16_t shunt_raw = static_cast<int16_t>(shunt_raw_u);
            result.power_valid = true;
            result.servo_bus_v = static_cast<float>((bus_raw >> 3) * 4) / 1000.0f;
            // Common INA219 modules use a 0.1-ohm shunt. 10uV/bit / 0.1ohm = 0.1mA/bit.
            result.servo_current_ma = static_cast<float>(shunt_raw) * 0.1f;
        }
    }

    if (aht20_) {
        uint8_t trigger[3] = {0xAC, 0x33, 0x00};
        uint8_t data[7]{};
        if (i2c_master_transmit(aht20_, trigger, sizeof(trigger), IO_TIMEOUT_MS) == ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(90));
            if (i2c_master_receive(aht20_, data, sizeof(data), IO_TIMEOUT_MS) == ESP_OK && (data[0] & 0x80) == 0) {
                const uint32_t humidity_raw = (static_cast<uint32_t>(data[1]) << 12) |
                                              (static_cast<uint32_t>(data[2]) << 4) |
                                              (static_cast<uint32_t>(data[3]) >> 4);
                const uint32_t temp_raw = (static_cast<uint32_t>(data[3] & 0x0F) << 16) |
                                          (static_cast<uint32_t>(data[4]) << 8) |
                                          static_cast<uint32_t>(data[5]);
                result.environment_valid = true;
                result.humidity_percent = static_cast<float>(humidity_raw) * 100.0f / 1048576.0f;
                result.temperature_c = static_cast<float>(temp_raw) * 200.0f / 1048576.0f - 50.0f;
            }
        }
    }
    return result;
}
