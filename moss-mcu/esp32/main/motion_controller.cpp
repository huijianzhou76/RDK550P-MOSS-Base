#include "motion_controller.h"

#include <algorithm>
#include <cmath>

#include "esp_log.h"
#include "freertos/task.h"

namespace {
constexpr uint32_t SERVO_PERIOD_US = 20000;
constexpr uint32_t SERVO_CENTER_US = 1500;
constexpr uint32_t SERVO_RANGE_US = 500;
constexpr uint32_t SERVO_DUTY_BITS = 14;
constexpr float PI_F = 3.14159265358979323846f;
constexpr int MOTION_TICK_MS = 20;
constexpr int QUEUE_DEPTH = 8;
const char *TAG = "MOSS_MOTION";
}

uint32_t MotionController::pulse_to_duty(uint32_t pulse_us) {
    constexpr uint32_t max_duty = (1u << SERVO_DUTY_BITS) - 1;
    return static_cast<uint32_t>((static_cast<uint64_t>(pulse_us) * max_duty) / SERVO_PERIOD_US);
}

uint32_t MotionController::angle_to_pulse(float angle_deg) {
    const float clamped = std::max(-90.0f, std::min(90.0f, angle_deg));
    return static_cast<uint32_t>(SERVO_CENTER_US + (clamped / 90.0f) * SERVO_RANGE_US);
}

bool MotionController::init(int yaw_gpio, int pitch_gpio) {
    ledc_timer_config_t timer = {};
    timer.speed_mode = LEDC_LOW_SPEED_MODE;
    // ESP32-S3 exposes up to LEDC_TIMER_14_BIT in ESP-IDF 5.4.
    // 14-bit at 50Hz still provides far more than enough servo pulse resolution.
    timer.duty_resolution = LEDC_TIMER_14_BIT;
    timer.timer_num = LEDC_TIMER_0;
    timer.freq_hz = 50;
    timer.clk_cfg = LEDC_AUTO_CLK;
    if (ledc_timer_config(&timer) != ESP_OK) return false;

    ledc_channel_config_t yaw = {};
    yaw.gpio_num = yaw_gpio;
    yaw.speed_mode = LEDC_LOW_SPEED_MODE;
    yaw.channel = LEDC_CHANNEL_0;
    yaw.intr_type = LEDC_INTR_DISABLE;
    yaw.timer_sel = LEDC_TIMER_0;
    yaw.duty = pulse_to_duty(SERVO_CENTER_US);
    yaw.hpoint = 0;
    if (ledc_channel_config(&yaw) != ESP_OK) return false;

    ledc_channel_config_t pitch = yaw;
    pitch.gpio_num = pitch_gpio;
    pitch.channel = LEDC_CHANNEL_1;
    if (ledc_channel_config(&pitch) != ESP_OK) return false;

    queue_ = xQueueCreate(QUEUE_DEPTH, sizeof(MotionCommand));
    if (!queue_) return false;

    write_pose(0.0f, 0.0f);
    BaseType_t ok = xTaskCreate(worker_entry, "moss_motion", 4096, this, 7, nullptr);
    return ok == pdPASS;
}

bool MotionController::enqueue(float yaw_deg, float pitch_deg, float speed) {
    if (!queue_ || !enabled_.load()) return false;
    MotionCommand cmd{
        yaw_deg,
        pitch_deg,
        std::max(0.05f, std::min(1.0f, speed)),
    };
    if (uxQueueSpacesAvailable(queue_) == 0) {
        MotionCommand discarded{};
        xQueueReceive(queue_, &discarded, 0);
    }
    return xQueueSend(queue_, &cmd, 0) == pdTRUE;
}

bool MotionController::center(float speed) {
    return enqueue(0.0f, 0.0f, speed);
}

void MotionController::emergency_stop() {
    enabled_.store(false);
    if (queue_) xQueueReset(queue_);
    set_pwm_enabled(false);
}

void MotionController::resume() {
    enabled_.store(true);
    set_pwm_enabled(true);
    HeadPose current = pose();
    write_pose(current.yaw_deg, current.pitch_deg);
}

HeadPose MotionController::pose() const {
    HeadPose result{};
    portENTER_CRITICAL(&pose_mux_);
    result.yaw_deg = yaw_deg_;
    result.pitch_deg = pitch_deg_;
    portEXIT_CRITICAL(&pose_mux_);
    return result;
}

void MotionController::set_pwm_enabled(bool enabled) {
    if (!enabled) {
        ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, 0);
        ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, 0);
        return;
    }
    HeadPose current = pose();
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, pulse_to_duty(angle_to_pulse(current.yaw_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, pulse_to_duty(angle_to_pulse(current.pitch_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
}

void MotionController::write_pose(float yaw_deg, float pitch_deg) {
    if (!enabled_.load()) return;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, pulse_to_duty(angle_to_pulse(yaw_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, pulse_to_duty(angle_to_pulse(pitch_deg)));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
    portENTER_CRITICAL(&pose_mux_);
    yaw_deg_ = yaw_deg;
    pitch_deg_ = pitch_deg;
    portEXIT_CRITICAL(&pose_mux_);
}

void MotionController::worker_entry(void *arg) {
    static_cast<MotionController *>(arg)->worker();
}

void MotionController::worker() {
    MotionCommand cmd{};
    while (true) {
        if (xQueueReceive(queue_, &cmd, portMAX_DELAY) != pdTRUE) continue;
        if (!enabled_.load()) continue;

        const HeadPose start = pose();
        const float distance = std::max(std::fabs(cmd.yaw_deg - start.yaw_deg), std::fabs(cmd.pitch_deg - start.pitch_deg));
        const float deg_per_sec = 25.0f + cmd.speed * 155.0f;
        const float duration_sec = std::max(0.12f, distance / deg_per_sec);
        const int steps = std::max(1, static_cast<int>((duration_sec * 1000.0f) / MOTION_TICK_MS));

        ESP_LOGI(TAG, "move yaw %.1f->%.1f pitch %.1f->%.1f speed %.2f steps %d",
                 start.yaw_deg, cmd.yaw_deg, start.pitch_deg, cmd.pitch_deg, cmd.speed, steps);

        for (int i = 1; i <= steps; ++i) {
            if (!enabled_.load()) break;
            const float t = static_cast<float>(i) / static_cast<float>(steps);
            const float eased = 0.5f - 0.5f * std::cos(PI_F * t);
            const float yaw = start.yaw_deg + (cmd.yaw_deg - start.yaw_deg) * eased;
            const float pitch = start.pitch_deg + (cmd.pitch_deg - start.pitch_deg) * eased;
            write_pose(yaw, pitch);
            vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));
        }
    }
}
