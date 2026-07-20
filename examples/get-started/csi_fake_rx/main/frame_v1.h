/*
 * CSI 바이너리 프레임 v1 — Realtime/FRAME_SPEC.md의 C 구현.
 * 서버측 구현(Realtime/csi_frame.py)과 반드시 일치해야 한다.
 *
 * 레이아웃 (총 436B, 리틀엔디언 = ESP32 네이티브):
 *   [0:2]   magic A5 5A
 *   [2]     version 0x01
 *   [3]     예약
 *   [4:50]  메타 46B (tx_id, flags, seq u32, ts u64, rssi, noise, ch, csi_len u16)
 *   [50:434] CSI i8×384
 *   [434:436] CRC16-CCITT(FALSE) — [4:434] 대상
 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define FRAME_MAGIC0     0xA5
#define FRAME_MAGIC1     0x5A
#define FRAME_VERSION    0x01
#define FRAME_HEADER_LEN 4
#define FRAME_META_LEN   46
#define FRAME_CSI_LEN    384
#define FRAME_CRC_LEN    2
#define FRAME_LEN        436   /* 4 + 46 + 384 + 2 */

/* CRC16-CCITT(FALSE): poly 0x1021, init 0xFFFF */
static inline uint16_t frame_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                                 : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static inline void frame_build(uint8_t out[FRAME_LEN],
                               uint8_t tx_id, uint8_t flags,
                               uint32_t seq, uint64_t rx_timestamp_us,
                               int8_t rssi, int8_t noise_floor, uint8_t channel,
                               const int8_t csi[FRAME_CSI_LEN])
{
    memset(out, 0, FRAME_LEN);
    out[0] = FRAME_MAGIC0;
    out[1] = FRAME_MAGIC1;
    out[2] = FRAME_VERSION;
    out[3] = 0x00;
    out[4] = tx_id;
    out[5] = flags;
    memcpy(&out[6],  &seq, 4);
    memcpy(&out[10], &rx_timestamp_us, 8);
    out[18] = (uint8_t)rssi;
    out[19] = (uint8_t)noise_floor;
    out[20] = channel;
    uint16_t csi_len = FRAME_CSI_LEN;
    memcpy(&out[21], &csi_len, 2);
    /* [23:50] 예약 = 0 (memset) */
    memcpy(&out[50], csi, FRAME_CSI_LEN);
    uint16_t crc = frame_crc16(&out[FRAME_HEADER_LEN],
                               FRAME_META_LEN + FRAME_CSI_LEN);
    memcpy(&out[FRAME_LEN - FRAME_CRC_LEN], &crc, 2);
}
