#pragma once

#include <atomic>
#include <cstdint>

#include "driver/ledc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

struct HeadPose {
    float yaw_deg;
    float pitch_deg;
};

struct MotionCommand {
    float yaw_deg;
    float pitch_deg;
    float speed;
};

class MotionController {
public:
    bool init(int yaw_gpio, int pitch_gpio);
    bool enqueue(float yaw_deg, float pitch_deg, float speed);
    bool center(float speed = 0.45f);
    void emergency_stop();
    void resume();
    bool enabled() const { return enabled_.load(); }
    HeadPose pose() const;

private:
    static void worker_entry(void *arg);
    void worker();
    void write_pose(float yaw_deg, float pitch_deg);
    void set_pwm_enabled(bool enabled);
    static uint32_t pulse_to_duty(uint32_t pulse_us);
    static uint32_t angle_to_pulse(float angle_deg);

    QueueHandle_t queue_ = nullptr;
    std::atomic<bool> enabled_{true};
    mutable portMUX_TYPE pose_mux_ = portMUX_INITIALIZER_UNLOCKED;
    float yaw_deg_ = 0.0f;
    float pitch_deg_ = 0.0f;
};
