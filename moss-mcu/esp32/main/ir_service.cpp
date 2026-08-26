#include "ir_service.h"

#include <algorithm>
#include <cstring>

#include "driver/rmt_encoder.h"
#include "esp_log.h"
#include "freertos/task.h"

namespace {
const char *TAG = "MOSS_IR";
constexpr uint32_t RMT_RESOLUTION_HZ = 1000000;  // 1 tick = 1 us
constexpr uint32_t IR_CARRIER_HZ = 38000;
}

bool IrService::init(int rx_gpio, int tx_gpio) {
    rmt_rx_channel_config_t rx_config = {};
    rx_config.gpio_num = static_cast<gpio_num_t>(rx_gpio);
    rx_config.clk_src = RMT_CLK_SRC_DEFAULT;
    rx_config.resolution_hz = RMT_RESOLUTION_HZ;
    rx_config.mem_block_symbols = 64;
    if (rmt_new_rx_channel(&rx_config, &rx_channel_) != ESP_OK) return false;

    rmt_rx_event_callbacks_t callbacks = {};
    callbacks.on_recv_done = rx_done_callback;
    if (rmt_rx_register_event_callbacks(rx_channel_, &callbacks, this) != ESP_OK) return false;
    if (rmt_enable(rx_channel_) != ESP_OK) return false;

    rmt_tx_channel_config_t tx_config = {};
    tx_config.gpio_num = static_cast<gpio_num_t>(tx_gpio);
    tx_config.clk_src = RMT_CLK_SRC_DEFAULT;
    tx_config.resolution_hz = RMT_RESOLUTION_HZ;
    tx_config.mem_block_symbols = 64;
    tx_config.trans_queue_depth = 4;
    if (rmt_new_tx_channel(&tx_config, &tx_channel_) != ESP_OK) return false;
    if (rmt_enable(tx_channel_) != ESP_OK) return false;

    rmt_carrier_config_t carrier = {};
    carrier.frequency_hz = IR_CARRIER_HZ;
    carrier.duty_cycle = 0.33f;
    carrier.flags.polarity_active_low = false;
    carrier.flags.always_on = false;
    if (rmt_apply_carrier(tx_channel_, &carrier) != ESP_OK) return false;

    rmt_copy_encoder_config_t copy_config = {};
    if (rmt_new_copy_encoder(&copy_config, &copy_encoder_) != ESP_OK) return false;

    rx_done_ = xSemaphoreCreateBinary();
    if (!rx_done_) return false;

    ready_ = true;
    ESP_LOGI(TAG, "IR ready rx=%d tx=%d carrier=%uHz", rx_gpio, tx_gpio, IR_CARRIER_HZ);
    return true;
}

bool IrService::rx_done_callback(rmt_channel_handle_t,
                                 const rmt_rx_done_event_data_t *edata,
                                 void *user_ctx) {
    auto *self = static_cast<IrService *>(user_ctx);
    self->rx_count_ = std::min<size_t>(edata->num_symbols, MAX_SYMBOLS);
    BaseType_t high_task_wakeup = pdFALSE;
    xSemaphoreGiveFromISR(self->rx_done_, &high_task_wakeup);
    return high_task_wakeup == pdTRUE;
}

IrService::Slot *IrService::find_slot(const std::string &name) {
    for (auto &slot : slots_) {
        if (slot.used && slot.name == name) return &slot;
    }
    return nullptr;
}

IrService::Slot *IrService::find_or_allocate_slot(const std::string &name) {
    if (auto *existing = find_slot(name)) return existing;
    for (auto &slot : slots_) {
        if (!slot.used) {
            slot.used = true;
            slot.name = name;
            slot.count = 0;
            return &slot;
        }
    }
    return nullptr;
}

bool IrService::learn(const std::string &slot_name, int timeout_ms, size_t *symbol_count) {
    if (!ready_ || slot_name.empty()) return false;
    auto *slot = find_or_allocate_slot(slot_name.substr(0, 16));
    if (!slot) return false;

    while (xSemaphoreTake(rx_done_, 0) == pdTRUE) {}
    rx_count_ = 0;

    rmt_receive_config_t receive_config = {};
    receive_config.signal_range_min_ns = 1000;
    receive_config.signal_range_max_ns = 12000000;
    if (rmt_receive(rx_channel_, rx_buffer_, sizeof(rx_buffer_), &receive_config) != ESP_OK) return false;

    if (xSemaphoreTake(rx_done_, pdMS_TO_TICKS(std::max(500, timeout_ms))) != pdTRUE) {
        ESP_LOGW(TAG, "IR learn timeout for slot %s", slot_name.c_str());
        return false;
    }
    if (rx_count_ == 0) return false;

    slot->count = std::min<size_t>(rx_count_, MAX_SYMBOLS);
    // Typical 38kHz demodulating receivers output LOW while a carrier burst is
    // present. RMT TX carrier is applied to HIGH levels, so invert the learned
    // levels while preserving durations.
    for (size_t i = 0; i < slot->count; ++i) {
        slot->symbols[i] = rx_buffer_[i];
        slot->symbols[i].level0 = !rx_buffer_[i].level0;
        slot->symbols[i].level1 = !rx_buffer_[i].level1;
    }
    if (symbol_count) *symbol_count = slot->count;
    ESP_LOGI(TAG, "learned slot=%s symbols=%u", slot->name.c_str(), static_cast<unsigned>(slot->count));
    return true;
}

bool IrService::send(const std::string &slot_name, int repeat) {
    if (!ready_) return false;
    auto *slot = find_slot(slot_name);
    if (!slot || slot->count == 0) return false;

    rmt_transmit_config_t transmit_config = {};
    transmit_config.loop_count = 0;
    const int count = std::max(1, std::min(5, repeat));
    for (int i = 0; i < count; ++i) {
        if (rmt_transmit(tx_channel_, copy_encoder_, slot->symbols,
                         slot->count * sizeof(rmt_symbol_word_t), &transmit_config) != ESP_OK) {
            return false;
        }
        if (rmt_tx_wait_all_done(tx_channel_, 1000) != ESP_OK) return false;
        if (i + 1 < count) vTaskDelay(pdMS_TO_TICKS(120));
    }
    return true;
}

std::vector<std::string> IrService::slots() const {
    std::vector<std::string> result;
    for (const auto &slot : slots_) {
        if (slot.used && slot.count > 0) result.push_back(slot.name);
    }
    return result;
}
