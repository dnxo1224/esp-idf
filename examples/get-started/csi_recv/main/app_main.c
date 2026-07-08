/*
 * Optimized CSI Receiver
 * - 링버퍼 + 별도 출력 태스크로 패킷 손실 최소화
 * - 콜백에서는 큐에 넣기만 하고 즉시 리턴
 * - 출력은 별도 태스크에서 처리
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "nvs_flash.h"
#include "esp_mac.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"

// 타겟에 따라 gain control 헤더 포함
#if CONFIG_IDF_TARGET_ESP32S3 || CONFIG_IDF_TARGET_ESP32C3 || CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
#include "esp_csi_gain_ctrl.h"
#define CONFIG_GAIN_CONTROL 1
#else
#define CONFIG_GAIN_CONTROL 0
#endif

// ==================== 설정 ====================
#define CONFIG_LESS_INTERFERENCE_CHANNEL    11
#define CONFIG_ESP_NOW_PHYMODE              WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE                 WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                   0

// 큐 설정
#define CSI_QUEUE_SIZE                      400     // 큐 크기 (버퍼 개수)
#define CSI_PRINT_TASK_STACK                8192    // 출력 태스크 스택 크기
#define CSI_PRINT_TASK_PRIORITY             5       // 출력 태스크 우선순위

// 대역폭 설정
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61 || (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0))
#define CONFIG_WIFI_BAND_MODE               WIFI_BAND_MODE_2G_ONLY
#define CONFIG_WIFI_2G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_5G_BANDWIDTHS           WIFI_BW_HT40
#define CONFIG_WIFI_2G_PROTOCOL             WIFI_PROTOCOL_11N
#define CONFIG_WIFI_5G_PROTOCOL             WIFI_PROTOCOL_11N
#else
#define CONFIG_WIFI_BANDWIDTH               WIFI_BW_HT40
#endif

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
#define CSI_FORCE_LLTF                      0
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#define ESP_IF_WIFI_STA ESP_MAC_WIFI_STA
#endif

// ==================== 전역 변수 ====================
static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const char *TAG = "csi_recv";
static uint8_t s_my_mac[6] = {0};

// CSI 데이터 저장 구조체
typedef struct {
    wifi_pkt_rx_ctrl_t rx_ctrl;
    uint8_t mac[6];
    int8_t buf[384];
    uint16_t len;
    uint8_t first_word_invalid;
    uint32_t rx_id;
    float compensate_gain;
} csi_data_t;

// 큐 핸들
static QueueHandle_t s_csi_queue = NULL;

// 통계
static volatile uint32_t s_total_received = 0;
static volatile uint32_t s_total_dropped = 0;

// ==================== Wi-Fi 초기화 ====================
static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

#if CONFIG_IDF_TARGET_ESP32C5
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
        .ghz_5g = CONFIG_WIFI_5G_PROTOCOL
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
        .ghz_5g = CONFIG_WIFI_5G_BANDWIDTHS
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_band_mode(CONFIG_WIFI_BAND_MODE);
    wifi_protocols_t protocols = {
        .ghz_2g = CONFIG_WIFI_2G_PROTOCOL,
    };
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_IF_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {
        .ghz_2g = CONFIG_WIFI_2G_BANDWIDTHS,
    };
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_IF_WIFI_STA, &bandwidth));
#else
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(ESP_IF_WIFI_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
#endif

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    
#if CONFIG_IDF_TARGET_ESP32C5
    if ((CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20)
            || (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_5G_ONLY && CONFIG_WIFI_5G_BANDWIDTHS == WIFI_BW_HT20)) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#elif (CONFIG_IDF_TARGET_ESP32C6 && ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 4, 0)) || CONFIG_IDF_TARGET_ESP32C61
    if (CONFIG_WIFI_BAND_MODE == WIFI_BAND_MODE_2G_ONLY && CONFIG_WIFI_2G_BANDWIDTHS == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#else
    if (CONFIG_WIFI_BANDWIDTH == WIFI_BW_HT20) {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_NONE));
    } else {
        ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    }
#endif
}

// ==================== ESP-NOW 초기화 ====================
static void wifi_esp_now_init(esp_now_peer_info_t peer)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
}

// ==================== CSI 콜백 (최소 작업만!) ====================
static void IRAM_ATTR wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        return;
    }

    // MAC 필터링
    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6)) {
        return;
    }

    s_total_received++;

    // CSI 데이터 복사
    csi_data_t csi_data;
    memcpy(&csi_data.rx_ctrl, &info->rx_ctrl, sizeof(wifi_pkt_rx_ctrl_t));
    memcpy(csi_data.mac, info->mac, 6);
    
    // CSI 버퍼 복사 (길이 제한)
    uint16_t copy_len = (info->len > 384) ? 384 : info->len;
    memcpy(csi_data.buf, info->buf, copy_len);
    csi_data.len = copy_len;
    csi_data.first_word_invalid = info->first_word_invalid;
    csi_data.rx_id = *(uint32_t *)(info->payload + 15);
    csi_data.compensate_gain = 1.0f;

#if CONFIG_GAIN_CONTROL
    // Gain 보상 계산
    uint8_t agc_gain = 0;
    int8_t fft_gain = 0;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc_gain, &fft_gain);
    esp_csi_gain_ctrl_get_gain_compensation(&csi_data.compensate_gain, agc_gain, fft_gain);
#endif

    // 큐에 넣기 (블로킹 없이)
    BaseType_t ret = xQueueSendFromISR(s_csi_queue, &csi_data, NULL);
    if (ret != pdTRUE) {
        s_total_dropped++;
    }
}

// ==================== CSI 출력 태스크 ====================
static void csi_print_task(void *arg)
{
    csi_data_t csi_data;
    static uint32_t s_print_count = 0;

    ESP_LOGI(TAG, "CSI print task started");

    while (1) {
        // 큐에서 데이터 대기
        if (xQueueReceive(s_csi_queue, &csi_data, portMAX_DELAY) == pdTRUE) {
            const wifi_pkt_rx_ctrl_t *rx_ctrl = &csi_data.rx_ctrl;

            // 첫 번째 데이터일 때 헤더 출력
            if (s_print_count == 0) {
                ESP_LOGI(TAG, "================ CSI RECV ================");
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
                ets_printf("type,recv_mac,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_state,len,first_word,data\n");
#else
                ets_printf("type,recv_mac,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data\n");
#endif
            }

            // 메타데이터 출력
#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C6 || CONFIG_IDF_TARGET_ESP32C61
            ets_printf("CSI_DATA," MACSTR ",%lu," MACSTR ",%d,%d,%d,%d,%d,%d,%lu,%d,%d",
                       MAC2STR(s_my_mac), 
                       (unsigned long)csi_data.rx_id, 
                       MAC2STR(csi_data.mac), 
                       rx_ctrl->rssi, 
                       rx_ctrl->rate,
                       rx_ctrl->noise_floor, 
                       0,  // fft_gain
                       0,  // agc_gain
                       rx_ctrl->channel,
                       (unsigned long)rx_ctrl->timestamp, 
                       rx_ctrl->sig_len, 
                       rx_ctrl->rx_state);
#else
            ets_printf("CSI_DATA," MACSTR ",%lu," MACSTR ",%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%lu,%d,%d,%d",
                       MAC2STR(s_my_mac), 
                       (unsigned long)csi_data.rx_id, 
                       MAC2STR(csi_data.mac), 
                       rx_ctrl->rssi, 
                       rx_ctrl->rate, 
                       rx_ctrl->sig_mode,
                       rx_ctrl->mcs, 
                       rx_ctrl->cwb, 
                       rx_ctrl->smoothing, 
                       rx_ctrl->not_sounding,
                       rx_ctrl->aggregation, 
                       rx_ctrl->stbc, 
                       rx_ctrl->fec_coding, 
                       rx_ctrl->sgi,
                       rx_ctrl->noise_floor, 
                       rx_ctrl->ampdu_cnt, 
                       rx_ctrl->channel, 
                       rx_ctrl->secondary_channel,
                       (unsigned long)rx_ctrl->timestamp, 
                       rx_ctrl->ant, 
                       rx_ctrl->sig_len, 
                       rx_ctrl->rx_state);
#endif

            // CSI 데이터 출력
            ets_printf(",%d,%d,\"[%d", 
                       csi_data.len, 
                       csi_data.first_word_invalid, 
                       (int16_t)(csi_data.compensate_gain * csi_data.buf[0]));
            
            for (int i = 1; i < csi_data.len; i++) {
                ets_printf(",%d", (int16_t)(csi_data.compensate_gain * csi_data.buf[i]));
            }
            ets_printf("]\"\n");

            s_print_count++;

            // 1000개마다 통계 출력
            if (s_print_count % 1000 == 0) {
                ESP_LOGI(TAG, "Stats: printed=%lu, received=%lu, dropped=%lu, queue=%d/%d",
                         (unsigned long)s_print_count,
                         (unsigned long)s_total_received,
                         (unsigned long)s_total_dropped,
                         (int)uxQueueMessagesWaiting(s_csi_queue),
                         CSI_QUEUE_SIZE);
            }
        }
    }
}

// ==================== CSI 초기화 ====================
static void wifi_csi_init(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));

#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61
    wifi_csi_config_t csi_config = {
        .enable                   = true,
        .acquire_csi_legacy       = false,
        .acquire_csi_force_lltf   = CSI_FORCE_LLTF,
        .acquire_csi_ht20         = true,
        .acquire_csi_ht40         = true,
        .acquire_csi_vht          = false,
        .acquire_csi_su           = false,
        .acquire_csi_mu           = false,
        .acquire_csi_dcm          = false,
        .acquire_csi_beamformed   = false,
        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg            = 0,
        .dump_ack_en              = false,
        .reserved                 = false
    };
#elif CONFIG_IDF_TARGET_ESP32C6
    wifi_csi_config_t csi_config = {
        .enable                 = true,
        .acquire_csi_legacy     = false,
        .acquire_csi_ht20       = true,
        .acquire_csi_ht40       = true,
        .acquire_csi_su         = true,
        .acquire_csi_mu         = true,
        .acquire_csi_dcm        = true,
        .acquire_csi_beamformed = true,
        .acquire_csi_he_stbc    = 2,
        .val_scale_cfg          = false,
        .dump_ack_en            = false,
        .reserved               = false
    };
#else
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
#endif

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

// ==================== 메인 ====================
void app_main(void)
{
    // NVS 초기화
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Wi-Fi 초기화
    wifi_init();

    // ESP-NOW 초기화
    esp_now_peer_info_t peer = {
        .channel   = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx     = WIFI_IF_STA,
        .encrypt   = false,
        .peer_addr = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff},
    };
    wifi_esp_now_init(peer);

    // MAC 주소 가져오기
    esp_wifi_get_mac(WIFI_IF_STA, s_my_mac);
    ESP_LOGI(TAG, "Receiver MAC: " MACSTR, MAC2STR(s_my_mac));

    // CSI 큐 생성
    s_csi_queue = xQueueCreate(CSI_QUEUE_SIZE, sizeof(csi_data_t));
    if (s_csi_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create CSI queue!");
        return;
    }
    ESP_LOGI(TAG, "CSI queue created (size: %d)", CSI_QUEUE_SIZE);

    // 출력 태스크 생성
    BaseType_t task_ret = xTaskCreatePinnedToCore(
        csi_print_task,
        "csi_print",
        CSI_PRINT_TASK_STACK,
        NULL,
        CSI_PRINT_TASK_PRIORITY,
        NULL,
        1  // Core 1에서 실행 (Wi-Fi는 Core 0)
    );
    
    if (task_ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create CSI print task!");
        return;
    }

    // CSI 수신 시작
    wifi_csi_init();

    ESP_LOGI(TAG, "CSI receiver initialized. Waiting for data...");
}