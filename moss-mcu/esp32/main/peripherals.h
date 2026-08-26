#pragma once

#include <cstdint>
#include <string>

#include "driver/i2c_master.h"

struct SensorSnapshot {
    bool power_valid = false;
    float servo_bus_v = 0.0f;
    float servo_current_ma = 0.0f;
    bool environment_valid = false;
    float temperature_c = 0.0f;
    float humidity_percent = 0.0f;
};

struct PeripheralStatus {
    bool oled = false;
    bool ina219 = false;
    bool aht20 = false;
    bool eye_light = false;
};

class PeripheralHub {
public:
    bool init(int sda_gpio, int scl_gpio, int eye_pwm_gpio);
    PeripheralStatus status() const { return status_; }
    void set_eye(float brightness);
    bool display_text(const std::string &text);
    SensorSnapshot read_sensors();

private:
    bool init_i2c(int sda_gpio, int scl_gpio);
    bool init_oled();
    bool init_ina219();
    bool init_aht20();
    bool init_eye(int gpio);
    bool oled_command(uint8_t cmd);
    bool oled_data(const uint8_t *data, size_t len);
    void oled_clear_buffer();
    void draw_char(int x, int page, char ch);
    const uint8_t *glyph_for(char ch) const;
    bool ina219_read16(uint8_t reg, uint16_t *value);

    i2c_master_bus_handle_t bus_ = nullptr;
    i2c_master_dev_handle_t oled_ = nullptr;
    i2c_master_dev_handle_t ina219_ = nullptr;
    i2c_master_dev_handle_t aht20_ = nullptr;
    PeripheralStatus status_{};
    uint8_t oled_buffer_[1024]{};
};
