"use client";

import { Fragment, useEffect, useRef, useState } from "react";

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
const KIND_BADGE = {
  ok: "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300",
  check: "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300",
  fail: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300",
};
const KIND_ROW = {
  ok: "border-black/[.06] hover:bg-black/[.02] dark:border-white/[.08] dark:hover:bg-white/[.04]",
  check: "border-amber-200 bg-amber-50/60 hover:bg-amber-50 dark:border-amber-900/40 dark:bg-amber-950/30",
  fail: "border-red-200 bg-red-50/60 hover:bg-red-50 dark:border-red-900/40 dark:bg-red-950/30",
};
// 요건이 없다고 확인된 값은 흐리게, 사람이 봐야 하는 값은 강조해 한눈에 구분되게 한다
const NOT_REQUIRED_VALUES = new Set(["제한없음", "미적용"]);
const NEEDS_CHECK_VALUE = "[확인 필요]";

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
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 다른 탭에서 기다려도 진행·완료를 알 수 있게 탭 제목에 표시한다
  useEffect(() => {
    const base = "K-apt 낙찰 정보 수집";
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
  const filters: { key: Filter; label: string; count: number; tone: string }[] = [
    { key: "all", label: "전체", count: rows.length, tone: "text-black dark:text-zinc-50" },
    { key: "ok", label: "정상", count: successCount, tone: "text-green-700 dark:text-green-400" },
    { key: "check", label: "확인 필요", count: checkCount, tone: "text-amber-600 dark:text-amber-400" },
    { key: "fail", label: "수집 실패", count: failCount, tone: "text-red-600 dark:text-red-400" },
  ];
  const evidenceOf = (row: ResultRow) =>
    Object.entries(row)
      .filter(([k, v]) => k.startsWith("_근거_") && v)
      .map(([k, v]) => [k.slice("_근거_".length), v]);
  const q = query.trim();
  const visibleRows = rows
    .map((row, i) => ({ row, i, kind: rowKind(row) }))
    .filter(({ row, kind }) => (filter === "all" || filter === kind) && (!q || columns.some((c) => row[c]?.includes(q))));

  const rangeDays = (new Date(dateEnd).getTime() - new Date(dateStart).getTime()) / 86_400_000;
  const today = toDateInputValue(new Date());
  const activeQuickRange = QUICK_RANGES.find((r) => {
    const start = new Date();
    start.setDate(start.getDate() - r.days);
    return dateEnd === today && dateStart === toDateInputValue(start);
  })?.label;

  const inputClass =
    "w-full rounded-lg border border-black/[.08] bg-transparent px-3 py-2 text-sm text-black outline-none focus:border-black/40 dark:border-white/[.145] dark:text-zinc-50";
  const labelClass = "mb-2 block text-sm font-medium text-black dark:text-zinc-50";

  const cellTone = (value: string) =>
    NOT_REQUIRED_VALUES.has(value)
      ? "text-zinc-400 dark:text-zinc-500"
      : value.startsWith(NEEDS_CHECK_VALUE)
        ? "text-amber-700 dark:text-amber-400"
        : "text-zinc-700 dark:text-zinc-300";

  return (
    <div className="flex min-h-screen justify-center bg-zinc-50 px-3 py-12 sm:px-4 dark:bg-black">
      <main className={`w-full ${rows.length > 0 ? "max-w-[1600px]" : "max-w-3xl"} rounded-2xl border border-black/[.08] bg-white p-6 sm:p-8 dark:border-white/[.145] dark:bg-zinc-900`}>
        <h1 className="text-xl font-semibold text-black dark:text-zinc-50">
          K-apt 낙찰 정보 수집
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          조건에 맞는 낙찰공고를 검색하고 엑셀로 내려받습니다.
        </p>

        <form onSubmit={handleSubmit} className="mt-6 grid gap-5 md:grid-cols-2">
          <fieldset>
            <legend className={labelClass}>지역</legend>
            <div className="flex h-[38px] items-center gap-4">
              {REGIONS.map((r) => (
                <label key={r} className="flex items-center gap-1.5 text-sm text-zinc-700 dark:text-zinc-300">
                  <input
                    type="checkbox"
                    checked={regions.includes(r)}
                    onChange={() => toggleRegion(r)}
                  />
                  {r}
                </label>
              ))}
            </div>
          </fieldset>

          <div>
            <label htmlFor="keyword" className={labelClass}>
              검색어
            </label>
            <input
              id="keyword"
              type="text"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="예: 아스콘"
              className={inputClass}
            />
          </div>

          <fieldset className="md:col-span-2">
            <legend className={labelClass}>기간 (최대 1년)</legend>
            <div className="flex flex-wrap items-center gap-2">
              {QUICK_RANGES.map((r) => (
                <button
                  key={r.label}
                  type="button"
                  aria-pressed={activeQuickRange === r.label}
                  onClick={() => applyQuickRange(r.days)}
                  className={
                    "rounded-full border px-3 py-1.5 text-xs transition-colors " +
                    (activeQuickRange === r.label
                      ? "border-foreground bg-foreground text-background"
                      : "border-black/[.08] text-zinc-600 hover:bg-black/[.04] dark:border-white/[.145] dark:text-zinc-300 dark:hover:bg-white/[.08]")
                  }
                >
                  최근 {r.label}
                </button>
              ))}
              <div className="flex min-w-[280px] flex-1 items-center gap-2">
                <input
                  type="date"
                  aria-label="시작일"
                  value={dateStart}
                  max={dateEnd}
                  onChange={(e) => setDateStart(e.target.value)}
                  className={inputClass}
                />
                <span className="text-sm text-zinc-500 dark:text-zinc-400">~</span>
                <input
                  type="date"
                  aria-label="종료일"
                  value={dateEnd}
                  min={dateStart}
                  max={today}
                  onChange={(e) => setDateEnd(e.target.value)}
                  className={inputClass}
                />
              </div>
            </div>
            {rangeDays > LONG_RANGE_DAYS && rangeDays <= 365 && (
              <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
                기간이 {Math.round(rangeDays)}일로 길어요. 수집에 수십 분이 걸리고, 짧은 시간에 여러 번 수집하면 K-apt 접속이
                일시적으로 제한될 수 있어요.
              </p>
            )}
          </fieldset>

          <button
            type="submit"
            disabled={status === "loading" || regions.length === 0 || !keyword.trim()}
            className="flex h-11 items-center justify-center rounded-full bg-foreground px-5 text-sm font-medium text-background transition-colors hover:bg-[#383838] disabled:opacity-50 md:col-span-2 dark:hover:bg-[#ccc]"
          >
            {status === "loading"
              ? `수집 중... (${formatElapsed(elapsed)} 경과)`
              : regions.length === 0
                ? "지역을 1개 이상 선택해 주세요"
                : !keyword.trim()
                  ? "검색어를 입력해 주세요"
                  : "검색 시작"}
          </button>
        </form>

        {status === "loading" && (
          <div className="mt-5 rounded-xl border border-black/[.06] bg-zinc-50 p-4 dark:border-white/[.08] dark:bg-zinc-950">
            <div className="flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-300">
              <span className="h-3 w-3 flex-none animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-600 dark:border-t-zinc-300" />
              {progress?.message ?? "작업을 시작하는 중"}
            </div>
            {progress && progress.total > 0 && (
              <>
                <div
                  role="progressbar"
                  aria-label="공고 처리 진행률"
                  aria-valuemin={0}
                  aria-valuemax={progress.total}
                  aria-valuenow={progress.done}
                  className="mt-3 h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
                >
                  <div
                    className="h-full rounded-full bg-zinc-700 transition-[width] dark:bg-zinc-300"
                    style={{ width: `${(progress.done / progress.total) * 100}%` }}
                  />
                </div>
                <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
                  {progress.done} / {progress.total}건 처리 ({Math.floor((progress.done / progress.total) * 100)}%)
                </p>
              </>
            )}
            <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
              이 창을 닫거나 새로고침해도 수집은 계속돼요. 다시 열면 이어서 보여드려요.
            </p>
          </div>
        )}

        {status === "error" && (
          <div role="alert" className="mt-5 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900/40 dark:bg-red-950/40 dark:text-red-300">
            {errorMsg}
            {errorMsg.includes("K-apt 접속이 제한") && (
              <a
                href={KAPT_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 block font-medium underline"
              >
                K-apt 열어서 제한 해제하기
              </a>
            )}
          </div>
        )}

        {status === "done" && jobId && (
          <section className="mt-8 border-t border-black/[.06] pt-6 dark:border-white/[.08]">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-base font-semibold text-black dark:text-zinc-50">수집 결과</h2>
              <a
                href={`${API_BASE}/api/collect/${jobId}/download`}
                className="inline-flex h-9 items-center justify-center rounded-full bg-foreground px-4 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
              >
                엑셀 다운로드
              </a>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
              {filters.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  aria-pressed={filter === f.key}
                  onClick={() => {
                    setFilter(f.key);
                    setOpenRow(null);
                  }}
                  className={
                    "rounded-xl border px-4 py-3 text-left transition-colors " +
                    (filter === f.key
                      ? "border-foreground ring-1 ring-foreground"
                      : "border-black/[.08] hover:bg-black/[.03] dark:border-white/[.145] dark:hover:bg-white/[.06]")
                  }
                >
                  <span className="block text-xs text-zinc-500 dark:text-zinc-400">{f.label}</span>
                  <span className={`block text-2xl font-semibold ${f.tone}`}>{f.count}</span>
                </button>
              ))}
            </div>
            {checkCount + failCount > 0 && (
              <p className="mt-3 text-xs text-zinc-500 dark:text-zinc-400">
                <strong className="text-amber-600 dark:text-amber-400">확인 필요</strong>는 근거 문장을,{" "}
                <strong className="text-red-600 dark:text-red-400">수집 실패</strong>는 원문을 직접 확인해 주세요. 행을 누르면 근거와 원문
                링크가 펼쳐져요.
              </p>
            )}

            {rows.length > 0 && (
              <>
                <div className="mt-4 flex items-center gap-2">
                  <input
                    type="search"
                    aria-label="결과 안에서 찾기"
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setOpenRow(null);
                    }}
                    placeholder="결과 안에서 찾기 (아파트명, 공사명, 낙찰업체 등)"
                    className={`${inputClass} max-w-md`}
                  />
                  <span className="whitespace-nowrap text-xs text-zinc-500 dark:text-zinc-400">{visibleRows.length}건 표시</span>
                </div>

                <div className="mt-3 max-h-[70vh] overflow-auto rounded-lg border border-black/[.08] dark:border-white/[.145]">
                  <table className="w-full text-left text-xs">
                    <thead className="sticky top-0 z-10 bg-zinc-100 dark:bg-zinc-800">
                      <tr>
                        {columns.map((c) => (
                          <th key={c} className="whitespace-nowrap px-2.5 py-2 font-medium text-black dark:text-zinc-50">
                            {c === "공고 링크" ? "원문" : c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {visibleRows.length === 0 && (
                        <tr>
                          <td colSpan={columns.length} className="px-3 py-8 text-center text-zinc-500 dark:text-zinc-400">
                            조건에 맞는 공고가 없어요.
                          </td>
                        </tr>
                      )}
                      {visibleRows.map(({ row, i, kind }) => {
                        const isOpen = openRow === i;
                        const evidence = evidenceOf(row);
                        return (
                          <Fragment key={i}>
                            <tr
                              onClick={() => setOpenRow(isOpen ? null : i)}
                              className={`cursor-pointer border-t ${KIND_ROW[kind]}`}
                            >
                              {columns.map((c) =>
                                c === "공고 링크" ? (
                                  <td key={c} className="whitespace-nowrap px-3 py-2">
                                    {row[c] ? (
                                      <a
                                        href={row[c]}
                                        onClick={(e) => e.stopPropagation()}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="text-blue-600 underline hover:text-blue-800 dark:text-blue-400"
                                      >
                                        보기
                                      </a>
                                    ) : (
                                      "-"
                                    )}
                                  </td>
                                ) : c === "상태" ? (
                                  <td key={c} className="px-3 py-2" title={row[c]}>
                                    <button
                                      type="button"
                                      aria-expanded={isOpen}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        setOpenRow(isOpen ? null : i);
                                      }}
                                      className="flex items-center gap-1.5 text-left"
                                    >
                                      <span className="text-zinc-400" aria-hidden>
                                        {isOpen ? "▾" : "▸"}
                                      </span>
                                      <span className={`whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ${KIND_BADGE[kind]}`}>
                                        {KIND_LABELS[kind]}
                                      </span>
                                    </button>
                                  </td>
                                ) : (
                                  <td
                                    key={c}
                                    className={`max-w-[150px] truncate px-2.5 py-2 ${cellTone(row[c] ?? "")}`}
                                    title={row[c]}
                                  >
                                    {row[c] || "-"}
                                  </td>
                                )
                              )}
                            </tr>
                            {isOpen && (
                              <tr className="border-t border-black/[.06] bg-zinc-50 dark:border-white/[.08] dark:bg-zinc-950">
                                <td colSpan={columns.length} className="px-4 py-4 text-xs text-zinc-700 dark:text-zinc-300">
                                  {/* 표가 옆으로 길어도 근거가 화면 왼쪽에 보이도록 고정 */}
                                  <div className="sticky left-4 max-w-[min(56rem,calc(100vw-6rem))]">
                                  <p className="font-medium text-black dark:text-zinc-50">{row["상태"]}</p>
                                  {evidence.length > 0 ? (
                                    <div className="mt-3 grid gap-2">
                                      {evidence.map(([col, text]) => (
                                        <div key={col} className="rounded-lg border border-black/[.06] bg-white p-3 dark:border-white/[.08] dark:bg-zinc-900">
                                          <span className="font-medium text-black dark:text-zinc-50">
                                            {col}: {row[col] || "-"}
                                          </span>
                                          <p className="mt-1 whitespace-pre-wrap break-all text-zinc-600 dark:text-zinc-400">{text}</p>
                                        </div>
                                      ))}
                                    </div>
                                  ) : (
                                    <p className="mt-2 text-zinc-500">공고문에서 뽑은 근거 문장이 없어요. 원문을 직접 확인해 주세요.</p>
                                  )}
                                  {row["공고 링크"] && (
                                    <a
                                      href={row["공고 링크"]}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      className="mt-3 inline-block font-medium text-blue-600 underline hover:text-blue-800 dark:text-blue-400"
                                    >
                                      K-apt 원문 보기
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
              </>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
