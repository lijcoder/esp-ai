#include "board.h"

#include "driver/gpio.h"
#include "esp_log.h"

static const char *TAG = "BOARD";

esp_err_t board_init(void)
{
    gpio_config_t btn_cfg = {
        .pin_bit_mask = 1ULL << BOARD_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&btn_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "button gpio config failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "pins: mic bclk=%d ws=%d din=%d | amp bclk=%d lrc=%d din=%d | led=%d button=%d",
             BOARD_MIC_BCLK_GPIO, BOARD_MIC_WS_GPIO, BOARD_MIC_DIN_GPIO,
             BOARD_AMP_BCLK_GPIO, BOARD_AMP_WS_GPIO, BOARD_AMP_DOUT_GPIO,
             BOARD_LED_GPIO, BOARD_BUTTON_GPIO);
    return ESP_OK;
}

bool board_button_pressed(void)
{
    return gpio_get_level((gpio_num_t)BOARD_BUTTON_GPIO) == 0;
}
