"use client";

import dynamic from "next/dynamic";

/**
 * Studio 的首帧状态直接读 sessionStorage（刷新不丢工作现场，见 lib/session.ts），
 * 而服务端没有这个存储，SSR 出来的树必然是「空设定」，客户端一水合就整棵换掉。
 * `ssr: false` 让它只在客户端挂载：不用在 effect 里补一次 setState 来追赶，
 * 也没有水合不一致的告警 —— Next 文档明确要求这个写法放在 Client Component 里。
 */
const Studio = dynamic(() => import("@/components/Studio"), {
  ssr: false,
  loading: () => (
    <div className="flex min-h-screen items-center justify-center text-sm text-zinc-500">
      正在恢复工作现场…
    </div>
  ),
});

export default function Home() {
  return <Studio />;
}
