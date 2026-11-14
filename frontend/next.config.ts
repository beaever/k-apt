import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 데스크톱 앱(desktop/)은 화면을 정적 파일로 만들어 수집 서버가 직접 내준다
  ...(process.env.DESKTOP_BUILD ? { output: "export" as const } : {}),
};

export default nextConfig;
