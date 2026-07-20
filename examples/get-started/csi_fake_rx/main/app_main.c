/*
 * csi_fake_rx — 가짜 Rx (3단계 검증용)
 *
 * 진짜 Rx(ESP32-C5)가 하게 될 일을 흉내 낸다:
 * 436B 바이너리 프레임을 UART1 TX(2M baud)로 연속 방출.
 *
 *   [이 보드] --UART1 TX(GPIO17)--> [csi_tx_relay RX(GPIO18)] --TCP--> 서버
 *
 * 서버에서 seq 연속성/CRC 실패 0을 확인하면 게이트 2+3 통과.
 * RATE_HZ를 132로 올리면 4Tx 만재 부하 시험.
 */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/uart.h"

#include "frame_v1.h"

/* ==================== 설정 ==================== */
#define FAKE_UART_NUM     UART_NUM_1
#define FAKE_UART_TX_PIN  17
#define FAKE_UART_BAUD    2000000
#define RATE_HZ           33            /* 33 = 1Tx분 / 132 = 4Tx 만재 부하 */

static const char *TAG = "csi_fake_rx";

static void gen_task(void *arg)
{
    static uint8_t frame[FRAME_LEN];
    static int8_t csi[FRAME_CSI_LEN];
    uint32_t seq = 0;

    TickType_t last = xTaskGetTickCount();
    TickType_t period = pdMS_TO_TICKS(1000 / RATE_HZ);
    if (period == 0) period = 1;

    while (1) {
        for (int k = 0; k < FRAME_CSI_LEN; k++) {
            csi[k] = (int8_t)((k + seq) & 0x7F);
        }
        frame_build(frame, seq % 4, 0, seq,
                    (uint64_t)esp_timer_get_time(),
                    -45, -96, 36, csi);
        uart_write_bytes(FAKE_UART_NUM, frame, FRAME_LEN);
        seq++;

        if (seq % (RATE_HZ * 5) == 0) {
            ESP_LOGI(TAG, "sent=%lu frames (%d Hz)", (unsigned long)seq, RATE_HZ);
        }
        vTaskDelayUntil(&last, period);
    }
}

void app_main(void)
{
    uart_config_t uc = {
        .baud_rate  = FAKE_UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(FAKE_UART_NUM, 4096, 16384, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(FAKE_UART_NUM, &uc));
    ESP_ERROR_CHECK(uart_set_pin(FAKE_UART_NUM,
                                 FAKE_UART_TX_PIN, UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

    ESP_LOGI(TAG, "가짜 Rx 시작: UART%d TX=GPIO%d, %d baud, %d Hz, frame=%dB",
             FAKE_UART_NUM, FAKE_UART_TX_PIN, FAKE_UART_BAUD, RATE_HZ, FRAME_LEN);

    xTaskCreate(gen_task, "gen", 4096, NULL, 5, NULL);
}
