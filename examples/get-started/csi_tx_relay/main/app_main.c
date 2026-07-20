/*
 * csi_tx_relay — 송신 ESP (ESP32-S3)
 *
 * 역할: UART로 들어오는 바이트를 해석 없이 그대로 서버로 TCP 릴레이한다.
 *       (프레임 경계 탐색·체크섬 검증은 전부 서버 tcp_source.py 담당)
 *
 *   [Rx ESP32-C5] --UART1 2M baud--> [이 보드] --WiFi STA + raw TCP--> [서버:5010]
 *
 * 장애 대응:
 *   - WiFi 끊김   → 자동 재접속 (esp_wifi_connect 재시도)
 *   - TCP 끊김    → 1초 백오프 후 재연결, 그동안 스트림버퍼에 버퍼링
 *   - 버퍼 가득   → 새 데이터 폐기 + 카운트 (유실은 서버 SyncEngine이 결측 처리)
 *
 * 테스트 모드 (TEST_FRAME_GEN=1):
 *   UART 대신 합성 프레임을 33Hz로 자체 생성해 전송 — 2단계(WiFi/TCP 골격) 검증용.
 *   3단계(UART 파이프)에서는 0으로 바꿔 빌드한다.
 */
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/stream_buffer.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/uart.h"

#include "frame_v1.h"

/* ==================== 설정 (환경에 맞게 수정) ==================== */
#define TEST_FRAME_GEN      1               /* 1: 합성 프레임 자체 생성(2단계) / 0: UART 릴레이(3단계) */

#define WIFI_SSID           "YOUR_SSID"     /* 2.4GHz 공유기/AP */
#define WIFI_PASS           "YOUR_PASS"
#define SERVER_IP           "192.168.0.10"  /* tcp_source.py가 도는 서버 IP */
#define SERVER_PORT         5010

#define RELAY_UART_NUM      UART_NUM_1
#define RELAY_UART_RX_PIN   18              /* 상대(Rx) TX와 연결 */
#define RELAY_UART_BAUD     2000000

#define TEST_GEN_HZ         33              /* 테스트 모드 프레임 레이트 */

#define STREAM_BUF_SIZE     (64 * 1024)     /* 재접속 동안 버퍼링 (~150프레임, 4.5초) */
#define TCP_CHUNK           1460            /* MSS 크기로 송신 */

static const char *TAG = "csi_tx_relay";

/* ==================== 상태 ==================== */
static EventGroupHandle_t s_evt;
#define EVT_WIFI_CONNECTED  BIT0

static StreamBufferHandle_t s_pipe;         /* 생산자(UART/테스트젠) → 소비자(TCP) */

static volatile uint32_t s_bytes_in = 0;    /* 파이프 투입 바이트 */
static volatile uint32_t s_bytes_sent = 0;  /* TCP 송신 성공 바이트 */
static volatile uint32_t s_drop_bytes = 0;  /* 버퍼 가득으로 버린 바이트 */
static volatile uint32_t s_wifi_retries = 0;
static volatile uint32_t s_tcp_reconnects = 0;
static volatile bool     s_tcp_up = false;
static volatile uint32_t s_gen_frames = 0;

/* ==================== 파이프 투입 (버퍼 가득 시 새 데이터 폐기) ==================== */
static void pipe_push(const uint8_t *data, size_t len)
{
    size_t sent = xStreamBufferSend(s_pipe, data, len, 0);
    s_bytes_in += sent;
    if (sent < len) {
        s_drop_bytes += len - sent;
    }
}

/* ==================== WiFi ==================== */
static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_evt, EVT_WIFI_CONNECTED);
        s_wifi_retries++;
        esp_wifi_connect();                 /* 무조건 재시도 */
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "WiFi 연결됨, IP: " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_evt, EVT_WIFI_CONNECTED);
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, WIFI_PASS, sizeof(wc.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());
}

/* ==================== TCP 송신 태스크 ==================== */
static void tcp_task(void *arg)
{
    uint8_t chunk[TCP_CHUNK];

    while (1) {
        /* WiFi 대기 */
        xEventGroupWaitBits(s_evt, EVT_WIFI_CONNECTED,
                            pdFALSE, pdTRUE, portMAX_DELAY);

        int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock < 0) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }

        struct sockaddr_in dst = { 0 };
        dst.sin_family = AF_INET;
        dst.sin_port = htons(SERVER_PORT);
        dst.sin_addr.s_addr = inet_addr(SERVER_IP);

        ESP_LOGI(TAG, "서버 연결 시도 %s:%d ...", SERVER_IP, SERVER_PORT);
        if (connect(sock, (struct sockaddr *)&dst, sizeof(dst)) != 0) {
            close(sock);
            s_tcp_reconnects++;
            vTaskDelay(pdMS_TO_TICKS(1000));    /* 백오프 */
            continue;
        }
        int one = 1;
        setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        s_tcp_up = true;
        ESP_LOGI(TAG, "서버 연결됨");

        while (1) {
            size_t n = xStreamBufferReceive(s_pipe, chunk, sizeof(chunk),
                                            pdMS_TO_TICKS(100));
            if (n == 0) continue;

            size_t off = 0;
            bool fail = false;
            while (off < n) {
                int w = send(sock, chunk + off, n - off, 0);
                if (w <= 0) { fail = true; break; }
                off += w;
            }
            s_bytes_sent += off;
            if (fail) break;
        }

        s_tcp_up = false;
        close(sock);
        s_tcp_reconnects++;
        ESP_LOGW(TAG, "TCP 끊김 — 1초 후 재연결");
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

#if TEST_FRAME_GEN
/* ==================== 테스트 프레임 생성 태스크 (2단계) ==================== */
static void test_gen_task(void *arg)
{
    static uint8_t frame[FRAME_LEN];
    static int8_t csi[FRAME_CSI_LEN];
    uint32_t seq = 0;

    ESP_LOGI(TAG, "테스트 프레임 생성 시작 (%d Hz)", TEST_GEN_HZ);
    TickType_t last = xTaskGetTickCount();

    while (1) {
        /* 합성 CSI: seq에 따라 변하는 패턴 (내용 무관, 식별만 가능하면 됨) */
        for (int k = 0; k < FRAME_CSI_LEN; k++) {
            csi[k] = (int8_t)((k + seq) & 0x7F);
        }
        frame_build(frame, seq % 4, 0, seq,
                    (uint64_t)esp_timer_get_time(),
                    -45, -96, 36, csi);
        pipe_push(frame, FRAME_LEN);
        s_gen_frames++;
        seq++;
        vTaskDelayUntil(&last, pdMS_TO_TICKS(1000 / TEST_GEN_HZ));
    }
}
#else
/* ==================== UART 수신 태스크 (3단계) ==================== */
static void uart_task(void *arg)
{
    static uint8_t buf[2048];

    uart_config_t uc = {
        .baud_rate  = RELAY_UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(RELAY_UART_NUM, 16384, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(RELAY_UART_NUM, &uc));
    ESP_ERROR_CHECK(uart_set_pin(RELAY_UART_NUM,
                                 UART_PIN_NO_CHANGE, RELAY_UART_RX_PIN,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_LOGI(TAG, "UART%d 수신 시작 (RX=GPIO%d, %d baud)",
             RELAY_UART_NUM, RELAY_UART_RX_PIN, RELAY_UART_BAUD);

    while (1) {
        int n = uart_read_bytes(RELAY_UART_NUM, buf, sizeof(buf),
                                pdMS_TO_TICKS(20));
        if (n > 0) {
            pipe_push(buf, n);
        }
    }
}
#endif

/* ==================== 상태 로그 태스크 ==================== */
static void status_task(void *arg)
{
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        ESP_LOGI(TAG,
                 "wifi=%s tcp=%s | in=%lu sent=%lu drop=%lu | "
                 "gen=%lu | wifi_retry=%lu tcp_reconn=%lu buf=%u",
                 (xEventGroupGetBits(s_evt) & EVT_WIFI_CONNECTED) ? "OK" : "DOWN",
                 s_tcp_up ? "OK" : "DOWN",
                 (unsigned long)s_bytes_in,
                 (unsigned long)s_bytes_sent,
                 (unsigned long)s_drop_bytes,
                 (unsigned long)s_gen_frames,
                 (unsigned long)s_wifi_retries,
                 (unsigned long)s_tcp_reconnects,
                 (unsigned)xStreamBufferBytesAvailable(s_pipe));
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    s_evt = xEventGroupCreate();
    s_pipe = xStreamBufferCreate(STREAM_BUF_SIZE, 1);
    configASSERT(s_pipe);

    wifi_init();

    xTaskCreate(tcp_task, "tcp", 4096, NULL, 5, NULL);
#if TEST_FRAME_GEN
    xTaskCreate(test_gen_task, "gen", 4096, NULL, 4, NULL);
#else
    xTaskCreate(uart_task, "uart", 4096, NULL, 6, NULL);
#endif
    xTaskCreate(status_task, "status", 3072, NULL, 2, NULL);

    ESP_LOGI(TAG, "csi_tx_relay 시작 (mode=%s)",
             TEST_FRAME_GEN ? "TEST_GEN" : "UART_RELAY");
}
