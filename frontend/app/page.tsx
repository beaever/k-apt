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
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

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

  // "_근거_자본금" 같은 "_" 키는 컬럼이 아니라 해당 셀에 마우스를 올렸을 때 보여줄 근거
  const columns = rows.length > 0 ? Object.keys(rows[0]).filter((c) => !c.startsWith("_")) : [];
  const filters: { key: Filter; label: string; count: number }[] = [
    { key: "all", label: "전체", count: rows.length },
    { key: "ok", label: "정상", count: successCount },
    { key: "check", label: "확인 필요", count: checkCount },
    { key: "fail", label: "수집 실패", count: failCount },
  ];
  const evidenceOf = (row: ResultRow) =>
    Object.entries(row)
      .filter(([k, v]) => k.startsWith("_근거_") && v)
      .map(([k, v]) => [k.slice("_근거_".length), v]);

  return (
    <div className="flex min-h-screen justify-center bg-zinc-50 px-4 py-16 dark:bg-black">
      <main className={`w-full ${rows.length > 0 ? "max-w-6xl" : "max-w-3xl"} rounded-2xl border border-black/[.08] bg-white p-8 dark:border-white/[.145] dark:bg-zinc-900`}>
        <h1 className="text-xl font-semibold text-black dark:text-zinc-50">
          K-apt 낙찰 정보 수집
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          조건에 맞는 낙찰공고를 검색하고 엑셀로 내려받습니다.
        </p>

        <form onSubmit={handleSubmit} className="mt-6 flex flex-col gap-5">
          <div>
            <label className="mb-2 block text-sm font-medium text-black dark:text-zinc-50">
              지역
            </label>
            <div className="flex gap-4">
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
          </div>

          <div>
            <label htmlFor="keyword" className="mb-2 block text-sm font-medium text-black dark:text-zinc-50">
              검색어
            </label>
            <input
              id="keyword"
              type="text"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="예: 아스콘"
              className="w-full rounded-lg border border-black/[.08] bg-transparent px-3 py-2 text-sm text-black outline-none focus:border-black/40 dark:border-white/[.145] dark:text-zinc-50"
            />
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-black dark:text-zinc-50">
              기간 (최대 1년)
            </label>
            <div className="mb-2 flex gap-2">
              {QUICK_RANGES.map((q) => (
                <button
                  key={q.label}
                  type="button"
                  onClick={() => applyQuickRange(q.days)}
                  className="rounded-full border border-black/[.08] px-3 py-1 text-xs text-zinc-600 transition-colors hover:bg-black/[.04] dark:border-white/[.145] dark:text-zinc-300 dark:hover:bg-white/[.08]"
                >
                  {q.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <input
                type="date"
                value={dateStart}
                onChange={(e) => setDateStart(e.target.value)}
                className="w-full rounded-lg border border-black/[.08] bg-transparent px-3 py-2 text-sm text-black outline-none focus:border-black/40 dark:border-white/[.145] dark:text-zinc-50"
              />
              <span className="text-sm text-zinc-500 dark:text-zinc-400">~</span>
              <input
                type="date"
                value={dateEnd}
                onChange={(e) => setDateEnd(e.target.value)}
                className="w-full rounded-lg border border-black/[.08] bg-transparent px-3 py-2 text-sm text-black outline-none focus:border-black/40 dark:border-white/[.145] dark:text-zinc-50"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={status === "loading" || regions.length === 0 || !keyword}
            className="mt-2 flex h-11 items-center justify-center rounded-full bg-foreground px-5 text-sm font-medium text-background transition-colors hover:bg-[#383838] disabled:opacity-50 dark:hover:bg-[#ccc]"
          >
            {status === "loading" ? `수집 중... (${elapsed}초 경과)` : "검색 시작"}
          </button>
        </form>

        {status === "loading" && (
          <div className="mt-4">
            <div className="flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
              <span className="h-3 w-3 flex-none animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-600 dark:border-t-zinc-300" />
              {progress?.message ?? "작업을 시작하는 중"}
              {progress && progress.total > 0 && ` (${progress.done}/${progress.total}건)`}
            </div>
            {progress && progress.total > 0 && (
              <div
                role="progressbar"
                aria-label="공고 처리 진행률"
                aria-valuemin={0}
                aria-valuemax={progress.total}
                aria-valuenow={progress.done}
                className="mt-2 h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
              >
                <div
                  className="h-full rounded-full bg-zinc-700 transition-[width] dark:bg-zinc-300"
                  style={{ width: `${(progress.done / progress.total) * 100}%` }}
                />
              </div>
            )}
          </div>
        )}

        {status === "error" && (
          <p className="mt-4 text-sm text-red-600 dark:text-red-400">{errorMsg}</p>
        )}

        {status === "done" && jobId && (
          <div className="mt-6">
            <div className="flex items-center justify-between">
              <p className="text-sm text-black dark:text-zinc-50">
                정상: <strong>{successCount}</strong>건
                {checkCount > 0 && (
                  <>
                    , 확인 필요: <strong className="text-amber-600 dark:text-amber-400">{checkCount}</strong>건
                  </>
                )}
                {failCount > 0 && (
                  <>
                    , 수집 실패: <strong className="text-red-600 dark:text-red-400">{failCount}</strong>건
                  </>
                )}
              </p>
              <a
                href={`${API_BASE}/api/collect/${jobId}/download`}
                className="inline-flex h-9 items-center justify-center rounded-full bg-foreground px-4 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
              >
                엑셀 다운로드
              </a>
            </div>

            {rows.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-2">
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
                      "rounded-full border px-3 py-1 text-xs transition-colors " +
                      (filter === f.key
                        ? "border-foreground bg-foreground text-background"
                        : "border-black/[.08] text-zinc-600 hover:bg-black/[.04] dark:border-white/[.145] dark:text-zinc-300 dark:hover:bg-white/[.08]")
                    }
                  >
                    {f.label} {f.count}
                  </button>
                ))}
                <span className="self-center text-xs text-zinc-500 dark:text-zinc-400">
                  행을 누르면 판단 근거를 볼 수 있어요
                </span>
              </div>
            )}

            {rows.length > 0 && (
              <div className="mt-3 max-h-[70vh] overflow-auto rounded-lg border border-black/[.08] dark:border-white/[.145]">
                <table className="w-full text-left text-xs">
                  <thead className="sticky top-0 bg-zinc-100 dark:bg-zinc-800">
                    <tr>
                      {columns.map((c) => (
                        <th key={c} className="whitespace-nowrap px-3 py-2 font-medium text-black dark:text-zinc-50">
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, i) => {
                      const kind = rowKind(row);
                      if (filter !== "all" && filter !== kind) return null;
                      const failed = kind === "fail";
                      const needsCheck = kind === "check";
                      const isOpen = openRow === i;
                      return (
                        <Fragment key={i}>
                        <tr
                          onClick={() => setOpenRow(isOpen ? null : i)}
                          className={
                            "cursor-pointer " +
                            (failed
                              ? "border-t border-red-200 bg-red-50 dark:border-red-900/40 dark:bg-red-950/40"
                              : needsCheck
                                ? "border-t border-amber-200 bg-amber-50 dark:border-amber-900/40 dark:bg-amber-950/40"
                                : "border-t border-black/[.06] hover:bg-black/[.02] dark:border-white/[.08] dark:hover:bg-white/[.04]")
                          }
                        >
                          {columns.map((c) =>
                            c === "공고 링크" ? (
                              <td key={c} className="px-3 py-2">
                                {row[c] ? (
                                  <a
                                    href={row[c]}
                                    onClick={(e) => e.stopPropagation()}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-blue-600 underline hover:text-blue-800 dark:text-blue-400"
                                  >
                                    원문 보기
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
                                  className={
                                    "inline-block max-w-[200px] truncate rounded-full px-2 py-0.5 text-[11px] font-medium " +
                                    (failed
                                      ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300"
                                      : needsCheck
                                        ? "bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300"
                                        : "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300")
                                  }
                                >
                                  {row[c] || "-"}
                                </button>
                              </td>
                            ) : (
                              <td
                                key={c}
                                className={
                                  "max-w-[220px] truncate px-3 py-2 text-zinc-700 dark:text-zinc-300" +
                                  (row[`_근거_${c}`] ? " cursor-help underline decoration-dotted" : "")
                                }
                                title={row[`_근거_${c}`] ?? row[c]}
                              >
                                {row[c] || "-"}
                              </td>
                            )
                          )}
                        </tr>
                        {isOpen && (
                          <tr className="border-t border-black/[.06] bg-zinc-50 dark:border-white/[.08] dark:bg-zinc-950">
                            <td colSpan={columns.length} className="px-4 py-3 text-xs text-zinc-700 dark:text-zinc-300">
                              <p className="font-medium text-black dark:text-zinc-50">{row["상태"]}</p>
                              {evidenceOf(row).map(([col, text]) => (
                                <div key={col} className="mt-2">
                                  <span className="font-medium">{col}</span>
                                  <p className="mt-0.5 whitespace-pre-wrap text-zinc-600 dark:text-zinc-400">{text}</p>
                                </div>
                              ))}
                              {evidenceOf(row).length === 0 && (
                                <p className="mt-2 text-zinc-500">공고문에서 뽑은 근거 문장이 없어요. 원문을 직접 확인해 주세요.</p>
                              )}
                              {row["공고 링크"] && (
                                <a
                                  href={row["공고 링크"]}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="mt-2 inline-block text-blue-600 underline hover:text-blue-800 dark:text-blue-400"
                                >
                                  K-apt 원문 보기
                                </a>
                              )}
                            </td>
                          </tr>
                        )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
