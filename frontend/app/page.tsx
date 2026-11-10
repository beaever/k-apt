"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8420";

const REGIONS = ["서울", "인천", "경기"];

// toISOString()은 UTC 기준이라 한국 시간 오전 9시 이전엔 하루 전 날짜가 나온다 -> 로컬 시간대로 보정
const toDateInputValue = (d: Date) =>
  new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);

const QUICK_RANGES = [
  { label: "1주일", days: 7 },
  { label: "1개월", days: 30 },
  { label: "1년", days: 365 },
];

// 이보다 긴 기간은 수집이 오래 걸리고 K-apt 접속이 제한될 수 있어 미리 알린다
const LONG_RANGE_DAYS = 90;
const KAPT_URL = "https://www.k-apt.go.kr";
const KIND_LABELS = { ok: "정상", check: "확인 필요", fail: "수집 실패" } as const;
const KIND_STYLE = {
  ok: { pill: "bg-ok-bg text-ok", dot: "bg-ok", row: "" },
  check: { pill: "bg-warn-bg text-warn", dot: "bg-warn", row: "bg-warn-bg/40" },
  fail: { pill: "bg-bad-bg text-bad", dot: "bg-bad", row: "bg-bad-bg/40" },
};
// 진행 메시지(백엔드 job.message)로 지금 어느 단계인지 표시한다
const STEPS = ["목록 검색", "공고문 분석", "엑셀 생성"];
const stepOf = (message?: string) => (message?.includes("상세정보") ? 1 : 0);

// 요건이 없다고 확인된 값은 흐리게, 사람이 봐야 하는 값은 강조해 한눈에 구분되게 한다
const NOT_REQUIRED_VALUES = new Set(["제한없음", "미적용"]);
const NEEDS_CHECK_VALUE = "[확인 필요]";

const NUMERIC_COLUMNS = new Set(["세대수", "낙찰금액"]);
const LINK_COLUMN = "공고 링크";
// 백엔드 step4_excel.GUIDE_KEY: 확인 필요 사유와 판단 기준 안내(줄바꿈 구분)
const GUIDE_KEY = "_확인안내";
const EMPTY_VALUE = "-";
// 상태는 글자순이 아니라 확인이 급한 순서(실패 -> 확인 필요 -> 정상)로 정렬한다
const KIND_RANK = { fail: 0, check: 1, ok: 2 };
const collator = new Intl.Collator("ko", { numeric: true });

const withoutKey = <T,>(obj: Record<string, T>, key: string) => {
  const next = { ...obj };
  delete next[key];
  return next;
};

type SortDir = "asc" | "desc";
type Sort = { col: string; dir: SortDir } | null;

// 필터 목록·정렬에 쓰는 셀 값. 상태는 사유를 뺀 구분(정상/확인 필요/수집 실패)으로 묶는다.
const cellValue = (row: ResultRow, col: string) =>
  col === "상태" ? KIND_LABELS[rowKind(row)] : row[col] || EMPTY_VALUE;

const compareRows = (a: ResultRow, b: ResultRow, { col, dir }: NonNullable<Sort>) => {
  const va = cellValue(a, col);
  const vb = cellValue(b, col);
  // 빈 값은 방향과 관계없이 항상 맨 뒤
  if (va === EMPTY_VALUE || vb === EMPTY_VALUE) return Number(va === EMPTY_VALUE) - Number(vb === EMPTY_VALUE);
  const order =
    col === "상태"
      ? KIND_RANK[rowKind(a)] - KIND_RANK[rowKind(b)]
      : NUMERIC_COLUMNS.has(col)
        ? Number(va.replace(/[^\d.]/g, "")) - Number(vb.replace(/[^\d.]/g, ""))
        : collator.compare(va, vb);
  return dir === "asc" ? order : -order;
};

const formatElapsed = (sec: number) => (sec < 60 ? `${sec}초` : `${Math.floor(sec / 60)}분 ${sec % 60}초`);

const POLL_INTERVAL_MS = 2000;
// 일시적인 연결 끊김 한 번으로 수집이 실패한 것처럼 보이지 않도록 연속 실패가 이만큼 쌓여야 중단
const POLL_MAX_FAILURES = 5;
// 새로고침해도 진행 중이거나 끝난 작업을 다시 보여주기 위해 작업 번호를 기억한다
const JOB_STORAGE_KEY = "kapt-job-id";
const SERVER_DOWN_MESSAGE =
  "수집 서버에 연결하지 못했어요. 실행 창(start.bat 또는 start.command)이 켜져 있는지 확인하고, 꺼져 있으면 다시 실행해 주세요.";

const saveJobId = (id: string | null) => {
  try {
    if (id) localStorage.setItem(JOB_STORAGE_KEY, id);
    else localStorage.removeItem(JOB_STORAGE_KEY);
  } catch {}
};

const errorMessage = async (res: Response) => {
  const body = await res.json().catch(() => ({}));
  const detail = body.detail;
  return typeof detail === "string"
    ? detail
    : Array.isArray(detail) && detail[0]?.msg
      ? detail[0].msg
      : `요청 실패 (${res.status})`;
};

type Status = "idle" | "loading" | "done" | "error";
type ResultRow = Record<string, string>;
type Progress = { message: string; done: number; total: number };
type Job = Progress & {
  status: "running" | "done" | "error";
  rows?: ResultRow[];
  success_count?: number;
  check_count?: number;
  fail_count?: number;
};
type Filter = "all" | "ok" | "check" | "fail";

const rowKind = (row: ResultRow): Exclude<Filter, "all"> =>
  row["상태"]?.startsWith("수집 실패") ? "fail" : row["상태"]?.startsWith("확인 필요") ? "check" : "ok";

const describeError = (err: unknown) =>
  // fetch는 서버에 아예 연결하지 못하면 TypeError("Failed to fetch" 등)를 던진다
  err instanceof TypeError ? SERVER_DOWN_MESSAGE : err instanceof Error ? err.message : "알 수 없는 오류";

export default function Home() {
  const [regions, setRegions] = useState<string[]>(["서울", "인천", "경기"]);
  const [keyword, setKeyword] = useState("아스콘");
  const [dateStart, setDateStart] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    return toDateInputValue(d);
  });
  const [dateEnd, setDateEnd] = useState(() => toDateInputValue(new Date()));
  const [status, setStatus] = useState<Status>("idle");
  const [jobId, setJobId] = useState<string | null>(null);
  const [rows, setRows] = useState<ResultRow[]>([]);
  const [successCount, setSuccessCount] = useState(0);
  const [checkCount, setCheckCount] = useState(0);
  const [failCount, setFailCount] = useState(0);
  const [errorMsg, setErrorMsg] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [openRow, setOpenRow] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  // 결과 표가 넓어 결과가 나오면 검색 조건 패널을 접는다 (큰 화면에서만, 작은 화면은 위아래로 쌓임)
  const [panelOpen, setPanelOpen] = useState(true);
  const [sort, setSort] = useState<Sort>(null);
  // 열별로 보여줄 값 목록. 열이 없으면 그 열은 거르지 않는다.
  const [colFilters, setColFilters] = useState<Record<string, string[]>>({});
  const [menu, setMenu] = useState<{ col: string; left: number; top: number } | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 다른 탭에서 기다려도 진행·완료를 알 수 있게 탭 제목에 표시한다
  useEffect(() => {
    const base = "K-apt 낙찰 정보 수집기";
    document.title =
      status === "loading"
        ? `수집 중${progress?.total ? ` ${progress.done}/${progress.total}` : ""} · ${base}`
        : status === "done"
          ? `수집 완료 · ${base}`
          : status === "error"
            ? `오류 · ${base}`
            : base;
  }, [status, progress]);

  useEffect(() => {
    if (status === "loading") {
      timerRef.current = setInterval(() => setElapsed((s) => s + 1), 1000);
    } else if (timerRef.current) {
      clearInterval(timerRef.current);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [status]);

  const applyQuickRange = (days: number) => {
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - days);
    setDateStart(toDateInputValue(start));
    setDateEnd(toDateInputValue(end));
  };

  const toggleRegion = (r: string) => {
    setRegions((prev) =>
      prev.includes(r) ? prev.filter((x) => x !== r) : [...prev, r]
    );
  };

  const showJob = (id: string, job: Job) => {
    setJobId(id);
    setRows(job.rows ?? []);
    setSuccessCount(job.success_count ?? 0);
    setCheckCount(job.check_count ?? 0);
    setFailCount(job.fail_count ?? 0);
    setFilter("all");
    setQuery("");
    setOpenRow(null);
    setPanelOpen(false);
    setSort(null);
    setColFilters({});
    setMenu(null);
    setStatus("done");
  };

  // 수집은 오래 걸려 백그라운드 작업으로 돌고, 여기서는 끝날 때까지 상태를 폴링한다
  const pollJob = async (id: string, isActive: () => boolean) => {
    let failures = 0;
    while (isActive()) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
      let jobRes: Response;
      try {
        jobRes = await fetch(`${API_BASE}/api/collect/${id}`);
      } catch (err) {
        if (++failures < POLL_MAX_FAILURES) continue;
        throw err;
      }
      if (!jobRes.ok) {
        // 404는 서버가 재시작돼 작업이 사라진 경우라 기다려도 돌아오지 않는다
        if (jobRes.status !== 404 && ++failures < POLL_MAX_FAILURES) continue;
        throw new Error(await errorMessage(jobRes));
      }
      failures = 0;
      const job: Job = await jobRes.json();
      if (!isActive()) return;
      if (job.status === "running") {
        setProgress({ message: job.message, done: job.done, total: job.total });
        continue;
      }
      if (job.status === "error") throw new Error(job.message);
      showJob(id, job);
      return;
    }
  };

  // 새로고침 전에 보던 작업이 서버에 남아 있으면 이어서 보여준다
  useEffect(() => {
    let active = true;
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(JOB_STORAGE_KEY);
    } catch {}
    if (!saved) return;
    const id = saved;
    (async () => {
      const res = await fetch(`${API_BASE}/api/collect/${id}`).catch(() => null);
      if (!active) return;
      if (!res?.ok) {
        // 서버가 꺼졌거나 재시작돼 작업이 없으면 빈 화면에서 새로 시작한다
        if (res) saveJobId(null);
        return;
      }
      const job: Job = await res.json();
      if (!active) return;
      if (job.status === "done") return showJob(id, job);
      if (job.status === "error") {
        setErrorMsg(job.message);
        setStatus("error");
        return;
      }
      setStatus("loading");
      setProgress({ message: job.message, done: job.done, total: job.total });
      try {
        await pollJob(id, () => active);
      } catch (err) {
        if (!active) return;
        setErrorMsg(describeError(err));
        setStatus("error");
      }
    })();
    // 개발 모드는 이 효과를 두 번 실행하므로 먼저 실행된 폴링을 멈춘다
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (dateEnd < dateStart) {
      setErrorMsg("종료일이 시작일보다 빠릅니다.");
      setStatus("error");
      return;
    }
    const rangeDays = (new Date(dateEnd).getTime() - new Date(dateStart).getTime()) / 86_400_000;
    if (rangeDays > 365) {
      setErrorMsg("검색 기간은 1년 이내여야 합니다.");
      setStatus("error");
      return;
    }

    setStatus("loading");
    setElapsed(0);
    setErrorMsg("");
    setJobId(null);
    setRows([]);
    setProgress(null);

    try {
      const res = await fetch(`${API_BASE}/api/collect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ regions, keyword, date_start: dateStart, date_end: dateEnd }),
      });
      if (!res.ok) throw new Error(await errorMessage(res));
      const { job_id } = await res.json();
      saveJobId(job_id);
      await pollJob(job_id, () => true);
    } catch (err) {
      setErrorMsg(describeError(err));
      setStatus("error");
    }
  };

  // "_근거_자본금" 같은 "_" 키는 컬럼이 아니라 행을 펼쳤을 때 보여줄 근거
  // 사용자가 가장 먼저 봐야 하는 상태를 맨 앞에, 원문 링크는 맨 뒤에 둔다
  const columns =
    rows.length > 0
      ? [
          "상태",
          ...Object.keys(rows[0]).filter((c) => !c.startsWith("_") && c !== "상태" && c !== "공고 링크"),
          "공고 링크",
        ]
      : [];
  const filters: { key: Filter; label: string; count: number; dot: string; tone: string }[] = [
    { key: "all", label: "전체", count: rows.length, dot: "bg-subtle", tone: "text-ink" },
    { key: "ok", label: "정상", count: successCount, dot: "bg-ok", tone: "text-ok" },
    { key: "check", label: "확인 필요", count: checkCount, dot: "bg-warn", tone: "text-warn" },
    { key: "fail", label: "수집 실패", count: failCount, dot: "bg-bad", tone: "text-bad" },
  ];
  const evidenceOf = (row: ResultRow) =>
    Object.entries(row)
      .filter(([k, v]) => k.startsWith("_근거_") && v)
      .map(([k, v]) => [k.slice("_근거_".length), v]);
  const q = query.trim();
  const visibleRows = rows
    .map((row, i) => ({ row, i, kind: rowKind(row) }))
    .filter(
      ({ row, kind }) =>
        (filter === "all" || filter === kind) &&
        (!q || columns.some((c) => row[c]?.includes(q))) &&
        Object.entries(colFilters).every(([c, allowed]) => allowed.includes(cellValue(row, c)))
    );
  if (sort) visibleRows.sort((a, b) => compareRows(a.row, b.row, sort));
  const columnValues = (col: string) => {
    const counts = new Map<string, number>();
    for (const row of rows) counts.set(cellValue(row, col), (counts.get(cellValue(row, col)) ?? 0) + 1);
    return [...counts.entries()].sort(([a], [b]) => collator.compare(a, b));
  };
  const toggleSort = (col: string) =>
    setSort((cur) => (cur?.col !== col ? { col, dir: "asc" } : cur.dir === "asc" ? { col, dir: "desc" } : null));
  const openMenu = (col: string, e: React.MouseEvent<HTMLButtonElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    setMenu((cur) =>
      cur?.col === col ? null : { col, left: Math.max(8, Math.min(rect.left, window.innerWidth - MENU_WIDTH - 8)), top: rect.bottom + 6 }
    );
  };
  const activeFilterCount = Object.keys(colFilters).length;

  const rangeDays = (new Date(dateEnd).getTime() - new Date(dateStart).getTime()) / 86_400_000;
  const today = toDateInputValue(new Date());
  const activeQuickRange = QUICK_RANGES.find((r) => {
    const start = new Date();
    start.setDate(start.getDate() - r.days);
    return dateEnd === today && dateStart === toDateInputValue(start);
  })?.label;
  const step = status === "loading" ? stepOf(progress?.message) : 0;
  const percent = progress?.total ? Math.floor((progress.done / progress.total) * 100) : 0;

  const card = "rounded-2xl border border-line bg-surface shadow-[0_1px_2px_rgba(16,24,40,0.04)]";
  const inputClass =
    "h-10 w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink outline-none transition-colors placeholder:text-subtle focus:border-primary focus:ring-2 focus:ring-primary/20";
  const labelClass = "mb-2 block text-[13px] font-semibold text-ink";
  const chip = (on: boolean) =>
    "h-9 rounded-lg border px-3 text-[13px] font-medium transition-all duration-150 active:scale-[0.96] " +
    (on ? "border-primary bg-primary-soft text-primary" : "border-line bg-surface text-muted hover:border-subtle hover:text-ink");

  const cellTone = (value: string) =>
    NOT_REQUIRED_VALUES.has(value)
      ? "text-subtle"
      : value.startsWith(NEEDS_CHECK_VALUE)
        ? "font-medium text-warn"
        : "text-ink";

  return (
    <div className="min-h-screen bg-bg">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-16 max-w-[1600px] items-center gap-3 px-4 sm:px-6">
          <LogoMark />
          <div className="min-w-0">
            <p className="truncate text-[15px] font-bold tracking-tight text-ink">K-apt 낙찰 정보 수집기</p>
            <p className="truncate text-xs text-muted">낙찰공고 검색 · 참가자격 요건 정리 · 엑셀 내려받기</p>
          </div>
        </div>
      </header>

      <div
        className={`mx-auto grid max-w-[1600px] gap-6 px-4 py-6 sm:px-6 ${panelOpen ? "lg:grid-cols-[340px_minmax(0,1fr)]" : ""}`}
      >
        <aside key={String(panelOpen)} className={`animate-fade-up lg:sticky lg:top-6 lg:self-start ${panelOpen ? "" : "lg:hidden"}`}>
          <form onSubmit={handleSubmit} className={`${card} p-5`}>
            <div className="flex items-center justify-between">
              <h2 className="text-base font-bold text-ink">검색 조건</h2>
              {rows.length > 0 && (
                <button
                  type="button"
                  onClick={() => setPanelOpen(false)}
                  className="hidden h-7 rounded-md px-2 text-xs font-medium text-muted hover:bg-surface-2 hover:text-ink lg:block"
                >
                  접기 ←
                </button>
              )}
            </div>
            <p className="mt-1 text-xs text-muted">조건을 정하고 검색을 누르면 공고문까지 자동으로 분석해요.</p>

            <fieldset className="mt-5">
              <legend className={labelClass}>지역</legend>
              <div className="grid grid-cols-3 gap-2">
                {REGIONS.map((r) => (
                  <button
                    key={r}
                    type="button"
                    aria-pressed={regions.includes(r)}
                    onClick={() => toggleRegion(r)}
                    className={chip(regions.includes(r))}
                  >
                    {regions.includes(r) && <CheckIcon />}
                    {r}
                  </button>
                ))}
              </div>
            </fieldset>

            <div className="mt-5">
              <label htmlFor="keyword" className={labelClass}>
                검색어
              </label>
              <div className="relative">
                <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-subtle">
                  <SearchIcon />
                </span>
                <input
                  id="keyword"
                  type="text"
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                  placeholder="예: 아스콘"
                  className={`${inputClass} pl-9`}
                />
              </div>
            </div>

            <fieldset className="mt-5">
              <legend className={labelClass}>기간 (최대 1년)</legend>
              <div className="grid grid-cols-3 gap-1 rounded-lg bg-surface-2 p-1 ring-1 ring-line">
                {QUICK_RANGES.map((r) => (
                  <button
                    key={r.label}
                    type="button"
                    aria-pressed={activeQuickRange === r.label}
                    onClick={() => applyQuickRange(r.days)}
                    className={
                      "h-8 rounded-md text-[13px] font-medium transition-all duration-200 active:scale-[0.96] " +
                      (activeQuickRange === r.label ? "bg-surface text-ink shadow-sm ring-1 ring-line" : "text-muted hover:text-ink")
                    }
                  >
                    최근 {r.label}
                  </button>
                ))}
              </div>
              <div className="mt-2 grid grid-cols-[1fr_auto_1fr] items-center gap-2">
                <input
                  type="date"
                  aria-label="시작일"
                  value={dateStart}
                  max={dateEnd}
                  onChange={(e) => setDateStart(e.target.value)}
                  className={`${inputClass} px-2 text-[13px]`}
                />
                <span className="text-sm text-subtle">~</span>
                <input
                  type="date"
                  aria-label="종료일"
                  value={dateEnd}
                  min={dateStart}
                  max={today}
                  onChange={(e) => setDateEnd(e.target.value)}
                  className={`${inputClass} px-2 text-[13px]`}
                />
              </div>
              {rangeDays > LONG_RANGE_DAYS && rangeDays <= 365 && (
                <div className="mt-3 flex animate-pop gap-2 rounded-lg bg-warn-bg p-3 text-xs leading-relaxed text-warn">
                  <AlertIcon />
                  <p>
                    {Math.round(rangeDays)}일은 수집에 수십 분이 걸려요. 짧은 시간에 여러 번 수집하면 K-apt 접속이 일시적으로 제한될 수
                    있어요.
                  </p>
                </div>
              )}
            </fieldset>

            <button
              type="submit"
              disabled={status === "loading" || regions.length === 0 || !keyword.trim()}
              className="mt-6 flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-primary text-sm font-semibold text-primary-fg shadow-sm transition-all duration-150 hover:bg-primary-hover hover:shadow-md active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:shadow-none"
            >
              {status === "loading" ? (
                <>
                  <Spinner /> 수집 중 · {formatElapsed(elapsed)}
                </>
              ) : regions.length === 0 ? (
                "지역을 1개 이상 선택해 주세요"
              ) : !keyword.trim() ? (
                "검색어를 입력해 주세요"
              ) : (
                <>
                  <SearchIcon /> 검색 시작
                </>
              )}
            </button>
            <p className="mt-3 text-center text-[11px] text-subtle">한 번에 하나씩 수집할 수 있어요 · 결과는 24시간 보관</p>
          </form>
        </aside>

        <main className="min-w-0">
          {status === "idle" && (
            <section className={`${card} flex animate-fade-up flex-col items-center px-6 py-16 text-center`}>
              <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary-soft text-primary">
                <SearchIcon size={26} />
              </span>
              <h2 className="mt-4 text-lg font-bold text-ink">검색 조건을 정하고 검색을 시작하세요</h2>
              <p className="mt-1 text-sm text-muted">공고마다 공고문을 열어 자본금·실적·공법번호를 정리해 드려요.</p>
              <ol className="mt-8 grid w-full max-w-2xl gap-3 text-left sm:grid-cols-3">
                {[
                  ["낙찰공고 검색", "지역·기간·검색어로 K-apt 낙찰공고를 모아요."],
                  ["공고문 분석", "첨부된 공고문(HWP·PDF)에서 참가자격을 읽어요."],
                  ["확인 후 내려받기", "확인이 필요한 건만 근거와 함께 보고 엑셀로 받아요."],
                ].map(([title, desc], i) => (
                  <li
                    key={title}
                    style={{ animationDelay: `${120 + i * 80}ms` }}
                    className="animate-fade-up rounded-xl border border-line bg-surface-2 p-4 transition-transform duration-200 hover:-translate-y-0.5"
                  >
                    <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand text-xs font-bold text-white">
                      {i + 1}
                    </span>
                    <p className="mt-3 text-sm font-semibold text-ink">{title}</p>
                    <p className="mt-1 text-xs leading-relaxed text-muted">{desc}</p>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {status === "loading" && (
            <section className={`${card} animate-fade-up p-6`}>
              <div className="flex flex-wrap items-end justify-between gap-4">
                <div>
                  <p className="text-xs font-semibold text-primary">수집 진행 중</p>
                  <p className="mt-1 text-3xl font-bold tabular-nums text-ink">
                    {progress?.total ? `${percent}%` : "준비 중"}
                  </p>
                </div>
                <p className="text-sm tabular-nums text-muted">
                  {progress?.total ? `${progress.done} / ${progress.total}건` : ""} · {formatElapsed(elapsed)} 경과
                </p>
              </div>
              <div
                role="progressbar"
                aria-label="공고 처리 진행률"
                aria-valuemin={0}
                aria-valuemax={progress?.total || 1}
                aria-valuenow={progress?.done ?? 0}
                className="mt-4 h-2.5 overflow-hidden rounded-full bg-surface-2 ring-1 ring-line"
              >
                <div
                  className="relative h-full overflow-hidden rounded-full bg-primary transition-[width] duration-700 ease-out"
                  style={{ width: `${percent}%` }}
                >
                  <span className="absolute inset-0 animate-shimmer bg-gradient-to-r from-transparent via-white/40 to-transparent" />
                </div>
              </div>
              <ol className="mt-6 grid gap-2 sm:grid-cols-3">
                {STEPS.map((name, i) => (
                  <li
                    key={name}
                    className={
                      "flex items-center gap-2 rounded-lg border px-3 py-2.5 text-[13px] transition-all duration-300 " +
                      (i === step
                        ? "border-primary bg-primary-soft font-semibold text-primary"
                        : i < step
                          ? "border-line text-ink"
                          : "border-line text-subtle")
                    }
                  >
                    {i < step ? <CheckIcon /> : i === step ? <Spinner /> : <span className="w-4 text-center">{i + 1}</span>}
                    {name}
                  </li>
                ))}
              </ol>
              <p className="mt-4 text-sm text-muted">{progress?.message ?? "작업을 시작하는 중"}</p>
              <p className="mt-4 rounded-lg bg-surface-2 px-3 py-2 text-xs text-muted">
                이 창을 닫거나 새로고침해도 수집은 계속돼요. 다시 열면 이어서 보여드려요.
              </p>
            </section>
          )}

          {status === "error" && (
            <section role="alert" className={`${card} flex animate-fade-up gap-4 border-bad/30 p-6`}>
              <span className="flex h-10 w-10 flex-none items-center justify-center rounded-full bg-bad-bg text-bad">
                <AlertIcon size={20} />
              </span>
              <div>
                <h2 className="text-base font-bold text-ink">수집하지 못했어요</h2>
                <p className="mt-1 text-sm leading-relaxed text-muted">{errorMsg}</p>
                {errorMsg.includes("K-apt 접속이 제한") && (
                  <a
                    href={KAPT_URL}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-4 inline-flex h-9 items-center rounded-lg border border-line px-3 text-sm font-medium text-ink hover:bg-surface-2"
                  >
                    K-apt 열어서 제한 해제하기 ↗
                  </a>
                )}
              </div>
            </section>
          )}

          {status === "done" && jobId && (
            <section className="flex animate-fade-up flex-col gap-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-lg font-bold text-ink">수집 결과</h2>
                  <p className="text-xs text-muted">낙찰공고 {rows.length}건을 공고일자 순으로 정리했어요</p>
                </div>
                <div className="flex gap-2">
                {!panelOpen && (
                  <button
                    type="button"
                    onClick={() => setPanelOpen(true)}
                    className="hidden h-10 items-center gap-2 rounded-lg border border-line bg-surface px-4 text-sm font-medium text-ink transition-all duration-150 hover:border-subtle active:scale-[0.97] lg:inline-flex"
                  >
                    <SearchIcon /> 조건 변경
                  </button>
                )}
                <a
                  href={`${API_BASE}/api/collect/${jobId}/download`}
                  className="inline-flex h-10 items-center gap-2 rounded-lg bg-primary px-4 text-sm font-semibold text-primary-fg shadow-sm transition-all duration-150 hover:bg-primary-hover hover:shadow-md active:scale-[0.97]"
                >
                  <DownloadIcon /> 엑셀 다운로드
                </a>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
                {filters.map((f, idx) => (
                  <button
                    key={f.key}
                    type="button"
                    style={{ animationDelay: `${idx * 60}ms` }}
                    aria-pressed={filter === f.key}
                    onClick={() => {
                      setFilter(f.key);
                      setOpenRow(null);
                    }}
                    className={
                      `${card} animate-fade-up p-4 text-left transition-all duration-200 hover:-translate-y-0.5 hover:shadow-md active:translate-y-0 active:scale-[0.99] ` +
                      (filter === f.key ? "border-primary ring-2 ring-primary/20" : "hover:border-subtle")
                    }
                  >
                    <span className="flex items-center gap-2 text-[13px] font-medium text-muted">
                      <span className={`h-2 w-2 rounded-full ${f.dot}`} />
                      {f.label}
                    </span>
                    <span className="mt-2 flex items-baseline gap-1.5">
                      <span className={`text-[28px] font-bold leading-none tabular-nums ${f.tone}`}>{f.count}</span>
                      <span className="text-xs text-subtle">
                        건{f.key !== "all" && rows.length > 0 ? ` · ${Math.round((f.count / rows.length) * 100)}%` : ""}
                      </span>
                    </span>
                  </button>
                ))}
              </div>

              {rows.length > 0 && (
                <div className={`${card} overflow-hidden`}>
                  <div className="flex flex-wrap items-center gap-3 border-b border-line p-3">
                    <div className="relative w-full max-w-sm">
                      <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-subtle">
                        <SearchIcon />
                      </span>
                      <input
                        type="search"
                        aria-label="결과 안에서 찾기"
                        value={query}
                        onChange={(e) => {
                          setQuery(e.target.value);
                          setOpenRow(null);
                        }}
                        placeholder="아파트명, 공사명, 낙찰업체로 찾기"
                        className={`${inputClass} h-9 pl-9`}
                      />
                    </div>
                    <span className="text-xs text-muted">{visibleRows.length}건</span>
                    {Object.entries(colFilters).map(([c, allowed]) => (
                      <button
                        key={c}
                        type="button"
                        onClick={() => setColFilters((cur) => withoutKey(cur, c))}
                        className="inline-flex h-7 animate-pop items-center gap-1 rounded-full bg-primary-soft px-2.5 text-xs font-medium text-primary transition-opacity hover:opacity-80"
                        aria-label={`${c} 필터 해제`}
                      >
                        {c === LINK_COLUMN ? "원문" : c}: {allowed.length <= 2 ? allowed.join(", ") : `${allowed.length}개`} ✕
                      </button>
                    ))}
                    {(activeFilterCount > 0 || sort) && (
                      <button
                        type="button"
                        onClick={() => {
                          setColFilters({});
                          setSort(null);
                        }}
                        className="h-7 rounded-md px-2 text-xs font-medium text-muted hover:bg-surface-2 hover:text-ink"
                      >
                        정렬·필터 초기화
                      </button>
                    )}
                    <span className="ml-auto hidden text-xs text-subtle xl:block">제목을 누르면 정렬, ▾를 누르면 필터 · 행을 누르면 근거</span>
                  </div>

                  <div className="max-h-[72vh] overflow-auto" onScroll={() => setMenu(null)}>
                    <table className="w-full text-left text-[13px]">
                      <thead className="sticky top-0 z-10 bg-surface-2 shadow-[inset_0_-1px_0_var(--line)]">
                        <tr>
                          {columns.map((c) => (
                            <th
                              key={c}
                              aria-sort={sort?.col === c ? (sort.dir === "asc" ? "ascending" : "descending") : undefined}
                              className="whitespace-nowrap px-1 py-1.5 text-xs font-semibold text-muted"
                            >
                              {c === LINK_COLUMN ? (
                                <span className="px-1">원문</span>
                              ) : (
                                <div className={`flex items-center ${NUMERIC_COLUMNS.has(c) ? "justify-end" : ""}`}>
                                  <button
                                    type="button"
                                    onClick={() => toggleSort(c)}
                                    title="눌러서 정렬 (오름차순 → 내림차순 → 해제)"
                                    className={`flex items-center gap-1 rounded px-1 py-1 transition-colors hover:bg-line/60 hover:text-ink ${sort?.col === c ? "text-primary" : ""}`}
                                  >
                                    {c}
                                    <SortIcon dir={sort?.col === c ? sort.dir : null} />
                                  </button>
                                  <button
                                    type="button"
                                    aria-label={`${c} 필터·정렬 메뉴`}
                                    aria-haspopup="dialog"
                                    aria-expanded={menu?.col === c}
                                    onClick={(e) => openMenu(c, e)}
                                    className={`flex h-6 w-5 items-center justify-center rounded transition-colors ${colFilters[c] ? "bg-primary-soft text-primary" : "text-subtle hover:bg-line/60 hover:text-ink"}`}
                                  >
                                    <FilterIcon active={!!colFilters[c]} />
                                  </button>
                                </div>
                              )}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {visibleRows.length === 0 && (
                          <tr>
                            <td colSpan={columns.length} className="px-3 py-12 text-center text-sm text-muted">
                              조건에 맞는 공고가 없어요.
                            </td>
                          </tr>
                        )}
                        {visibleRows.map(({ row, i, kind }, idx) => {
                          const isOpen = openRow === i;
                          const evidence = evidenceOf(row);
                          return (
                            <Fragment key={i}>
                              <tr
                                onClick={() => setOpenRow(isOpen ? null : i)}
                                // 화면에 처음 보이는 행들만 차례로 나타나게 하고, 나머지는 기다리지 않게 지연을 제한
                                style={{ animationDelay: `${Math.min(idx, 20) * 18}ms` }}
                                className={`animate-fade-up cursor-pointer border-t border-line transition-colors hover:bg-surface-2 ${KIND_STYLE[kind].row} ${isOpen ? "bg-surface-2" : ""}`}
                              >
                                {columns.map((c) =>
                                  c === "공고 링크" ? (
                                    <td key={c} className="whitespace-nowrap px-3 py-2.5">
                                      {row[c] ? (
                                        <a
                                          href={row[c]}
                                          onClick={(e) => e.stopPropagation()}
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="font-medium text-primary hover:underline"
                                        >
                                          보기 ↗
                                        </a>
                                      ) : (
                                        "-"
                                      )}
                                    </td>
                                  ) : c === "상태" ? (
                                    <td key={c} className="px-3 py-2.5" title={row[c]}>
                                      <button
                                        type="button"
                                        aria-expanded={isOpen}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          setOpenRow(isOpen ? null : i);
                                        }}
                                        className="flex items-center gap-2"
                                      >
                                        <ChevronIcon open={isOpen} />
                                        <span
                                          className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-semibold ${KIND_STYLE[kind].pill}`}
                                        >
                                          <span className={`h-1.5 w-1.5 rounded-full ${KIND_STYLE[kind].dot}`} />
                                          {KIND_LABELS[kind]}
                                        </span>
                                      </button>
                                    </td>
                                  ) : (
                                    <td
                                      key={c}
                                      className={`max-w-[140px] truncate px-2.5 py-2.5 ${cellTone(row[c] ?? "")} ${NUMERIC_COLUMNS.has(c) ? "text-right tabular-nums" : ""} ${c === "아파트명" ? "font-semibold" : ""}`}
                                      title={row[c]}
                                    >
                                      {row[c] || "-"}
                                    </td>
                                  )
                                )}
                              </tr>
                              {isOpen && (
                                <tr className="border-t border-line bg-surface-2">
                                  <td colSpan={columns.length} className="px-4 py-4">
                                    {/* 표가 옆으로 길어도 근거가 화면 왼쪽에 보이도록 고정 */}
                                    <div className="sticky left-4 max-w-[min(60rem,calc(100vw-4rem))] animate-fade-up">
                                      <p className={`inline-block rounded-lg px-3 py-2 text-[13px] font-medium ${KIND_STYLE[kind].pill}`}>
                                        {row["상태"]}
                                      </p>
                                      {row[GUIDE_KEY] && (
                                        <div className="mt-3 rounded-xl border border-warn/30 bg-warn-bg p-3">
                                          <p className="text-xs font-semibold text-warn">왜 확인이 필요한가요?</p>
                                          <ul className="mt-1.5 list-disc space-y-1.5 pl-4 text-[13px] leading-relaxed text-ink">
                                            {row[GUIDE_KEY].split("\n").map((line) => (
                                              <li key={line}>{line}</li>
                                            ))}
                                          </ul>
                                        </div>
                                      )}
                                      {evidence.length > 0 ? (
                                        <div className="mt-3 grid gap-2 md:grid-cols-2">
                                          {evidence.map(([col, text]) => (
                                            <div key={col} className="rounded-xl border border-line bg-surface p-3">
                                              <p className="text-xs text-muted">{col}</p>
                                              <p className={`mt-0.5 text-sm font-semibold ${cellTone(row[col] ?? "")}`}>{row[col] || "-"}</p>
                                              <p className="mt-2 whitespace-pre-wrap break-all text-xs leading-relaxed text-muted">{text}</p>
                                            </div>
                                          ))}
                                        </div>
                                      ) : (
                                        <p className="mt-3 text-sm text-muted">공고문에서 뽑은 근거 문장이 없어요. 원문을 직접 확인해 주세요.</p>
                                      )}
                                      {row["공고 링크"] && (
                                        <a
                                          href={row["공고 링크"]}
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="mt-3 inline-flex h-9 items-center rounded-lg border border-line bg-surface px-3 text-sm font-medium text-ink hover:border-subtle"
                                        >
                                          K-apt 원문 보기 ↗
                                        </a>
                                      )}
                                    </div>
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              {menu && (
                <ColumnMenu
                  key={menu.col}
                  col={menu.col}
                  left={menu.left}
                  top={menu.top}
                  values={columnValues(menu.col)}
                  allowed={colFilters[menu.col]}
                  sortDir={sort?.col === menu.col ? sort.dir : null}
                  onSort={(dir) => {
                    setSort(dir ? { col: menu.col, dir } : null);
                    setMenu(null);
                  }}
                  onApply={(allowed) => {
                    setColFilters((cur) => (allowed ? { ...cur, [menu.col]: allowed } : withoutKey(cur, menu.col)));
                    setOpenRow(null);
                    setMenu(null);
                  }}
                  onClose={() => setMenu(null)}
                />
              )}
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

// 도로 차선을 본뜬 로고
function LogoMark() {
  return (
    <span className="flex h-9 w-9 flex-none items-center justify-center rounded-xl bg-brand" aria-hidden>
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M6 2 3 18M14 2l3 16" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" />
        <path d="M10 3v3M10 9v2.5M10 14.5V17" stroke="var(--marker)" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </span>
  );
}

function SearchIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="7" cy="7" r="4.75" stroke="currentColor" strokeWidth="1.5" />
      <path d="m10.5 10.5 3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden className="mr-1 inline-block align-[-3px]">
      <path d="m3.5 8.5 3 3 6-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function AlertIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden className="flex-none">
      <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8 4.75v3.75" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="8" cy="11" r=".9" fill="currentColor" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M8 2.5v8m0 0-3-3m3 3 3-3M3 13.5h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className={`text-subtle transition-transform ${open ? "rotate-90" : ""}`}
    >
      <path d="m6 3.5 4.5 4.5L6 12.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Spinner() {
  return <span className="h-4 w-4 flex-none animate-spin rounded-full border-2 border-current border-t-transparent" aria-hidden />;
}

const MENU_WIDTH = 280;

// 엑셀 자동 필터처럼 열 값으로 정렬·거르기
function ColumnMenu({
  col,
  left,
  top,
  values,
  allowed,
  sortDir,
  onSort,
  onApply,
  onClose,
}: {
  col: string;
  left: number;
  top: number;
  values: [string, number][];
  allowed?: string[];
  sortDir: SortDir | null;
  onSort: (dir: SortDir | null) => void;
  onApply: (allowed?: string[]) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState(() => new Set(allowed ?? values.map(([v]) => v)));
  const shown = useMemo(() => values.filter(([v]) => v.includes(search.trim())), [values, search]);
  const allShownSelected = shown.every(([v]) => selected.has(v));

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      // 메뉴를 연 ▾ 버튼은 클릭으로 직접 닫으므로 여기서 닫으면 곧바로 다시 열린다
      const opener = (e.target as Element).closest?.("[aria-haspopup][aria-expanded='true']");
      if (!ref.current?.contains(e.target as Node) && !opener) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  const toggle = (v: string) =>
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(v)) next.delete(v);
      else next.add(v);
      return next;
    });
  const sortButton = (dir: SortDir, label: string) => (
    <button
      type="button"
      onClick={() => onSort(sortDir === dir ? null : dir)}
      className={`flex h-8 flex-1 items-center justify-center gap-1 rounded-md border text-xs font-medium ${sortDir === dir ? "border-primary bg-primary-soft text-primary" : "border-line text-ink hover:bg-surface-2"}`}
    >
      <SortIcon dir={dir} /> {label}
    </button>
  );

  return (
    <div
      ref={ref}
      role="dialog"
      aria-label={`${col} 정렬·필터`}
      style={{ left, top, width: MENU_WIDTH }}
      className="fixed z-50 origin-top-left animate-pop rounded-xl border border-line bg-surface p-3 text-sm text-ink shadow-[0_12px_32px_rgba(16,24,40,0.18)]"
    >
      <p className="text-xs font-semibold text-muted">{col}</p>
      <div className="mt-2 flex gap-2">
        {sortButton("asc", "오름차순")}
        {sortButton("desc", "내림차순")}
      </div>
      <div className="my-3 border-t border-line" />
      <input
        type="search"
        autoFocus
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="값 검색"
        aria-label={`${col} 값 검색`}
        className="h-8 w-full rounded-md border border-line bg-surface px-2.5 text-xs outline-none focus:border-primary"
      />
      <label className="mt-2 flex items-center gap-2 border-b border-line px-1 pb-2 text-xs font-medium">
        <input
          type="checkbox"
          checked={shown.length > 0 && allShownSelected}
          onChange={() =>
            setSelected((cur) => {
              const next = new Set(cur);
              for (const [v] of shown) {
                if (allShownSelected) next.delete(v);
                else next.add(v);
              }
              return next;
            })
          }
        />
        {search.trim() ? "검색 결과 모두 선택" : "모두 선택"}
      </label>
      <ul className="mt-1 max-h-56 overflow-auto">
        {shown.map(([v, count]) => (
          <li key={v}>
            <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-xs hover:bg-surface-2">
              <input type="checkbox" checked={selected.has(v)} onChange={() => toggle(v)} />
              <span className="min-w-0 flex-1 truncate" title={v}>
                {v === EMPTY_VALUE ? "(빈 값)" : v}
              </span>
              <span className="text-subtle tabular-nums">{count}</span>
            </label>
          </li>
        ))}
        {shown.length === 0 && <li className="px-1 py-3 text-center text-xs text-subtle">일치하는 값이 없어요</li>}
      </ul>
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={() => onApply(undefined)}
          className="h-8 flex-1 rounded-md border border-line text-xs font-medium hover:bg-surface-2"
        >
          필터 해제
        </button>
        <button
          type="button"
          disabled={selected.size === 0}
          onClick={() => onApply(selected.size === values.length ? undefined : [...selected])}
          className="h-8 flex-1 rounded-md bg-primary text-xs font-semibold text-primary-fg hover:bg-primary-hover disabled:opacity-50"
        >
          적용
        </button>
      </div>
    </div>
  );
}

function SortIcon({ dir }: { dir: SortDir | null }) {
  return (
    <svg width="10" height="12" viewBox="0 0 10 12" fill="none" aria-hidden className="flex-none">
      <path d="M5 1 8 4.5H2L5 1Z" fill="currentColor" opacity={dir === "asc" ? 1 : 0.3} />
      <path d="M5 11 2 7.5h6L5 11Z" fill="currentColor" opacity={dir === "desc" ? 1 : 0.3} />
    </svg>
  );
}

function FilterIcon({ active }: { active: boolean }) {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill={active ? "currentColor" : "none"} aria-hidden>
      <path d="M1.5 2h9L7 6.2V10L5 9V6.2L1.5 2Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  );
}
