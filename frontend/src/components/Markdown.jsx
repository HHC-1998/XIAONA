import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getToken } from '../api/client';

// Markdown 美化渲染（05 §12）：react-markdown + remark-gfm，样式贴合全站 indigo→violet 风格。
// 聊天/桌面助手的最终回复统一经此渲染，避免用户看到原始 Markdown 语法符号。
export function Markdown({ children }) {
  const text = typeof children === 'string' ? children : '';
  if (!text.trim()) return null;
  return (
    <div className="text-sm leading-relaxed break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ node, ...props }) => <p {...props} className="my-1.5 first:mt-0 last:mb-0" />,
          a: ({ node, href, children, ...props }) => {
            // 任务产物链接升级为「下载」按钮（04 §7 / 05 §7）：
            // 后端把产物以 [下载 xxx](/api/agent/tasks/{id}/artifacts/xxx) 形式写入消息，
            // 聊天页/桌面助手统一渲染成按钮样式。
            // 下载接口需 JWT（api/agent.py 的 get_current_user），裸 <a href> 导航不会带
            // Authorization 头（token 在 localStorage）→ 返回 401 新页面打不开。
            // 改为 fetch + Blob 触发浏览器保存到本机下载目录（2026-08-24 修）。
            if (typeof href === 'string' && href.startsWith('/api/agent/tasks/')) {
              const onDownload = async (e) => {
                e.preventDefault();
                try {
                  const res = await fetch(href, {
                    headers: { Authorization: `Bearer ${getToken()}` },
                  });
                  if (!res.ok) {
                    if (res.status === 401) {
                      window.alert('登录状态已失效，请重新登录后下载');
                    } else if (res.status === 404) {
                      window.alert('下载失败：文件可能已不存在（已过保留期被清理？）');
                    } else {
                      window.alert('下载失败，请稍后重试');
                    }
                    return;
                  }
                  const blob = await res.blob();
                  const filename = href.split('/').pop() || 'download';
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement('a');
                  a.href = url;
                  a.download = filename;
                  document.body.appendChild(a);
                  a.click();
                  a.remove();
                  URL.revokeObjectURL(url);
                } catch (err) {
                  console.error('artifact download failed:', err);
                  window.alert('网络异常，下载失败，请确认后端服务已启动');
                }
              };
              return (
                <a
                  {...props}
                  href={href}
                  onClick={onDownload}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 my-0.5 rounded-lg bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-700 shadow-sm"
                >
                  <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
                    <path d="M10 3a1 1 0 011 1v5.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 111.414-1.414L9 9.586V4a1 1 0 011-1z" />
                    <path d="M4 14a1 1 0 011 1v1a1 1 0 001 1h8a1 1 0 001-1v-1a1 1 0 112 0v1a3 3 0 01-3 3H6a3 3 0 01-3-3v-1a1 1 0 011-1z" />
                  </svg>
                  {children}
                </a>
              );
            }
            return (
              <a {...props} href={href} className="text-indigo-600 hover:underline" target="_blank" rel="noreferrer" />
            );
          },
          h1: ({ node, ...props }) => <h1 {...props} className="text-lg font-bold mt-3 mb-2" />,
          h2: ({ node, ...props }) => <h2 {...props} className="text-base font-bold mt-3 mb-1.5" />,
          h3: ({ node, ...props }) => <h3 {...props} className="text-sm font-bold mt-2 mb-1" />,
          h4: ({ node, ...props }) => <h4 {...props} className="text-sm font-semibold mt-2 mb-1" />,
          ul: ({ node, ...props }) => <ul {...props} className="list-disc pl-5 my-2 space-y-1" />,
          ol: ({ node, ...props }) => <ol {...props} className="list-decimal pl-5 my-2 space-y-1" />,
          li: ({ node, ...props }) => <li {...props} className="leading-relaxed" />,
          blockquote: ({ node, ...props }) => (
            <blockquote
              {...props}
              className="border-l-4 border-indigo-200 bg-indigo-50/50 px-3 py-1 my-2 rounded-r-md text-slate-600 italic"
            />
          ),
          hr: () => <hr className="my-3 border-slate-200" />,
          strong: ({ node, ...props }) => <strong {...props} className="font-semibold" />,
          em: ({ node, ...props }) => <em {...props} className="italic" />,
          code: ({ node, inline, className, children, ...props }) => {
            const isBlock = !inline;
            if (isBlock) {
              return (
                <code
                  {...props}
                  className="block bg-slate-900 text-slate-100 rounded-lg px-3 py-2 my-2 text-xs font-mono overflow-x-auto"
                >
                  {children}
                </code>
              );
            }
            return (
              <code
                {...props}
                className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 text-[0.85em] font-mono"
              >
                {children}
              </code>
            );
          },
          pre: ({ node, ...props }) => <pre {...props} className="my-2" />,
          table: ({ node, ...props }) => (
            <div className="overflow-x-auto my-2">
              <table {...props} className="min-w-full border-collapse text-xs" />
            </div>
          ),
          thead: ({ node, ...props }) => (
            <thead {...props} className="bg-slate-50 text-slate-700" />
          ),
          th: ({ node, ...props }) => (
            <th {...props} className="border border-slate-200 px-2 py-1.5 text-left font-semibold" />
          ),
          td: ({ node, ...props }) => (
            <td {...props} className="border border-slate-200 px-2 py-1.5" />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
