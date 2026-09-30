"use client";

import { useEffect, useRef, useState } from "react";

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

      // 수집은 오래 걸려 백그라운드 작업으로 돌고, 여기서는 완료될 때까지 상태를 폴링한다
      while (true) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        const jobRes = await fetch(`${API_BASE}/api/collect/${job_id}`);
        if (!jobRes.ok) throw new Error(await errorMessage(jobRes));
        const job = await jobRes.json();
        if (job.status === "running") {
          setProgress({ message: job.message, done: job.done, total: job.total });
          continue;
        }
        if (job.status === "error") throw new Error(job.message);
        setJobId(job_id);
        setRows(job.rows ?? []);
        setSuccessCount(job.success_count ?? 0);
        setCheckCount(job.check_count ?? 0);
        setFailCount(job.fail_count ?? 0);
        setStatus("done");
        break;
      }
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "알 수 없는 오류");
      setStatus("error");
    }
  };

  // "_근거_자본금" 같은 "_" 키는 컬럼이 아니라 해당 셀에 마우스를 올렸을 때 보여줄 근거
  const columns = rows.length > 0 ? Object.keys(rows[0]).filter((c) => !c.startsWith("_")) : [];

  return (
    <div className="flex min-h-screen justify-center bg-zinc-50 px-4 py-16 dark:bg-black">
      <main className="w-full max-w-3xl rounded-2xl border border-black/[.08] bg-white p-8 dark:border-white/[.145] dark:bg-zinc-900">
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
          <div className="mt-4 flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
            <span className="h-3 w-3 flex-none animate-spin rounded-full border-2 border-zinc-300 border-t-zinc-600 dark:border-zinc-600 dark:border-t-zinc-300" />
            {progress?.message ?? "작업을 시작하는 중"}
            {progress && progress.total > 0 && ` (${progress.done}/${progress.total}건)`}
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
              <div className="mt-3 max-h-96 overflow-auto rounded-lg border border-black/[.08] dark:border-white/[.145]">
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
                      const failed = row["상태"]?.startsWith("수집 실패");
                      const needsCheck = row["상태"]?.startsWith("확인 필요");
                      return (
                        <tr
                          key={i}
                          className={
                            failed
                              ? "border-t border-red-200 bg-red-50 dark:border-red-900/40 dark:bg-red-950/40"
                              : needsCheck
                                ? "border-t border-amber-200 bg-amber-50 dark:border-amber-900/40 dark:bg-amber-950/40"
                                : "border-t border-black/[.06] dark:border-white/[.08]"
                          }
                        >
                          {columns.map((c) =>
                            c === "공고 링크" ? (
                              <td key={c} className="px-3 py-2">
                                {row[c] ? (
                                  <a
                                    href={row[c]}
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
                                <span
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
                                </span>
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
