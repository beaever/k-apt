import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "K-apt 낙찰 정보 수집",
  description: "K-apt 아스콘 낙찰공고를 수집해 참가자격 요건을 정리하고 엑셀로 내려받습니다.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
