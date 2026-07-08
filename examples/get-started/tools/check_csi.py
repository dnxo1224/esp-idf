import os
import pandas as pd

def check_csi_hz():


    PERSON = 'lsi' \
    ''  # 검사할 폴더(이니셜) 입력
    


    ACTIONS = ['stand', 'sit', 'walk', 'handsup']
    POSITIONS = [str(i) for i in range(1, 17)] # '1' 부터 '16'까지
    RX_IDS = ['rx1', 'rx2', 'rx3', 'rx4']
    
    MIN_HZ = 24
    DURATION = 30
    # 합격 기준 프레임 수 (25Hz * 30초 = 750개)
    THRESHOLD_FRAMES = MIN_HZ * DURATION 

    print(f"==================================================")
    print(f"[*] 데이터 검증 시작 - 대상자: {PERSON}")
    print(f"[*] 기준: {MIN_HZ}Hz 이상 (30초 기준 최소 {THRESHOLD_FRAMES} 프레임)")
    print(f"==================================================\n")

    if not os.path.exists(PERSON):
        print(f"[!] '{PERSON}' 폴더를 찾을 수 없습니다. 경로를 확인해주세요.")
        return

    failed_sets = []
    total_sets = len(ACTIONS) * len(POSITIONS)
    checked_sets = 0
    missing_sets = 0

    for action in ACTIONS:
        for pos in POSITIONS:
            is_failed = False
            failed_reasons = []
            
            # 한 세트(Rx1~Rx4)의 파일 경로 생성
            for rx in RX_IDS:
                filename = f"{PERSON}_{action}_{pos}_{rx}.csv"
                filepath = os.path.join(PERSON, filename)
                
                # 파일이 아예 없는 경우
                if not os.path.exists(filepath):
                    is_failed = True
                    failed_reasons.append(f"{rx.upper()} 누락")
                    continue
                
                # CSV 파일의 행 개수(프레임 수) 확인
                try:
                    # header=None을 사용하여 첫 번째 데이터도 놓치지 않고 카운트
                    # 대용량 파일이 아니므로 pandas로 읽는 것이 간편합니다.
                    df = pd.read_csv(filepath, header=None)
                    frame_count = len(df)
                    
                    if frame_count < THRESHOLD_FRAMES:
                        is_failed = True
                        actual_hz = frame_count / DURATION
                        failed_reasons.append(f"{rx.upper()}: {actual_hz:.1f}Hz ({frame_count}개)")
                        
                except Exception as e:
                    # 파일이 비어있거나 읽을 수 없는 경우
                    is_failed = True
                    failed_reasons.append(f"{rx.upper()} 오류/빈 파일")

            if is_failed:
                # 누락된 세트와 Hz 미달 세트 구분
                if "누락" in str(failed_reasons):
                    missing_sets += 1
                else:
                    failed_sets.append({
                        'action': action,
                        'pos': pos,
                        'reason': ", ".join(failed_reasons)
                    })
            
            checked_sets += 1

    # 결과 출력
    print(f"[*] 총 {checked_sets}개 세트 중 {len(failed_sets)}개 세트 재수집 필요 (누락 {missing_sets}개)\n")
    
    if failed_sets:
        print(f"🚨 [재수집 필요 리스트 ({MIN_HZ}Hz 미만)]")
        for idx, item in enumerate(failed_sets, 1):
            print(f"  {idx}. 행동: {item['action']:<8} | 위치: {item['pos']:<2} | 문제 -> {item['reason']}")
    else:
        if missing_sets == 0:
            print(f"✅ 축하합니다! 모든 데이터가 {MIN_HZ}Hz 이상으로 완벽하게 수집되었습니다.")

if __name__ == '__main__':
    check_csi_hz()