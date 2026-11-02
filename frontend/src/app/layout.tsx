import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "MuseBlocks · 模块化 AI 音乐工作台",
  description: "Web 端模块化 AI 音乐生成系统 — 歌词分段、逐段 3 选 1、Suno 整段人声、硬切拼接",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="zh-CN"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full bg-[#07070c] font-sans text-zinc-100">{children}</body>
    </html>
  );
}
