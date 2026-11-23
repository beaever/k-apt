# v4/v5 글자 인식 벤치: 같은 스캔 공고를 번갈아 3회씩, 모델은 미리 불러 둔 상태에서 측정 (K-apt 접속 없음)
# 사용: backend/venv/bin/python docs/pr/faster-ocr/bench.py <공고 저장본 복사본 폴더>  (저장본 안의 .ocr.txt를 지우므로 원본이 아닌 복사본을 줄 것)
import glob, json, logging, os, sys, time
def main():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "backend"))
    from app.parser import step3_parse as p
    from app.export import step4_excel as x
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    import numpy as np, pypdfium2
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    root = sys.argv[1]
    bids = ["20261002093643337", "20261008110041182", "20261014132358981"]
    engines = {}
    for ver in ("v4", "v5"):
        e = RapidOCR(params={"Rec.lang_type": LangRec.KOREAN, "Rec.ocr_version": OCRVersion.PPOCRV4 if ver == "v4" else OCRVersion.PPOCRV5,
                             "Rec.model_type": ModelType.MOBILE, "Global.use_cls": False})
        e._load_det_model(); e._load_rec_model()
        engines[ver] = e
    pages = {}
    for bid in bids:
        d = json.load(open(os.path.join(root, bid, "detail.json"), encoding="utf-8"))
        pages[bid] = sum(len(pypdfium2.PdfDocument(os.path.join(root, bid, f))) for f in d["files"] if f.lower().endswith(".pdf"))
    times = {(v, b): [] for v in engines for b in bids}
    outputs = {}
    for rnd in range(3):
        for ver in (("v4", "v5") if rnd % 2 == 0 else ("v5", "v4")):
            p._ocr_engine = lambda e=engines[ver]: e
            for bid in bids:
                for f in glob.glob(f"{root}/{bid}/*.ocr.txt"): os.remove(f)
                d = json.load(open(os.path.join(root, bid, "detail.json"), encoding="utf-8"))
                files = [os.path.join(root, bid, f) for f in d["files"]]
                t = time.monotonic(); r = p.parse_files(files, d.get("download_error"), True); times[(ver, bid)].append(time.monotonic() - t)
                row = x.to_row_dict({"bid_num": bid, "success": True, "error": "", **d, **r})
                outputs[(ver, bid)] = (x.to_sheet_row(1, row), {k: v for k, v in row.items() if not k.startswith("_근거")}, {k: v for k, v in row.items() if k.startswith("_근거")})
    for bid in bids:
        a, b = times[("v4", bid)], times[("v5", bid)]
        print(f"{bid} ({pages[bid]}쪽) v4 {['%.1f'%t for t in a]} 중앙 {sorted(a)[1]:.1f} | v5 {['%.1f'%t for t in b]} 중앙 {sorted(b)[1]:.1f} | {sorted(a)[1]/sorted(b)[1]:.1f}배")
        same_sheet = outputs[("v4", bid)][0] == outputs[("v5", bid)][0]
        same_screen = outputs[("v4", bid)][1] == outputs[("v5", bid)][1]
        print(f"   엑셀 리스트 행 같음={same_sheet} / 화면 값(근거 제외) 같음={same_screen}")
        for k in outputs[("v4", bid)][2]:
            print(f"   {k}\n     v4: {outputs[('v4', bid)][2][k].splitlines()[0]}\n     v5: {outputs[('v5', bid)][2][k].splitlines()[0]}")
    s4 = sum(sorted(times[("v4", b)])[1] for b in bids); s5 = sum(sorted(times[("v5", b)])[1] for b in bids)
    print(f"합계(중앙값) v4 {s4:.1f}s v5 {s5:.1f}s {s4/s5:.1f}배")
    # 1쪽 단계별 시간
    f = glob.glob(f"{root}/20261008110041182/*.pdf")[0]
    img = pypdfium2.PdfDocument(f)[0].render(scale=3).to_numpy()[:, :, ::-1]
    for ver, e in engines.items():
        r = e(img)
        print(f"1쪽 단계별 {ver}: 탐지 {r.elapse_list[0]:.1f}s 글자읽기 {r.elapse_list[2]:.1f}s 줄 {len(r.txts)}")
if __name__ == "__main__":
    main()
