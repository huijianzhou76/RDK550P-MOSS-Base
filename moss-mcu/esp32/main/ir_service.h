#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "driver/rmt_rx.h"
#include "driver/rmt_tx.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

class IrService {
public:
    bool init(int rx_gpio, int tx_gpio);
    bool available() const { return ready_; }
    bool learn(const std::string &slot, int timeout_ms, size_t *symbol_count = nullptr);
    bool send(const std::string &slot, int repeat = 1);
    std::vector<std::string> slots() const;

private:
    static constexpr size_t MAX_SLOTS = 8;
    static constexpr size_t MAX_SYMBOLS = 256;

    struct Slot {
        bool used = false;
        std::string name;
        size_t count = 0;
        rmt_symbol_word_t symbols[MAX_SYMBOLS]{};
    };

    static bool rx_done_callback(rmt_channel_handle_t channel,
                                 const rmt_rx_done_event_data_t *edata,
                                 void *user_ctx);
    Slot *find_slot(const std::string &name);
    Slot *find_or_allocate_slot(const std::string &name);

    rmt_channel_handle_t rx_channel_ = nullptr;
    rmt_channel_handle_t tx_channel_ = nullptr;
    rmt_encoder_handle_t copy_encoder_ = nullptr;
    SemaphoreHandle_t rx_done_ = nullptr;
    rmt_symbol_word_t rx_buffer_[MAX_SYMBOLS]{};
    volatile size_t rx_count_ = 0;
    Slot slots_[MAX_SLOTS]{};
    bool ready_ = false;
};
