import { useState, useEffect, useRef } from 'react';
import { VoiceRecorder } from './VoiceRecorder';

// 前端预检（12 §2 与后端一致）：单文件 ≤10MB，一次 ≤5 个
const MAX_FILE_SIZE = 10 * 1024 * 1024;
const MAX_FILES = 5;

// 允许选择的类型（后端仍会二次判定；音频/视频会收到"无法识别"的聊天式反馈）
const ACCEPT = 'image/*,.pdf,.doc,.docx,.xlsx,.txt,.md,.csv,.json,.log,.yml,.yaml,.xml,.html,.py,.js,.ts,.java';

function formatSize(n) {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}MB`;
  if (n >= 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}

export function ChatInput({ onSend }) {
  const [text, setText] = useState('');
  const [files, setFiles] = useState([]);             // 待发送附件 [{file, name, size}]
  const [listening, setListening] = useState(false);  // 语音输入进行中
  const [micError, setMicError] = useState('');       // 语音/麦克风错误提示（3 秒自动消失）
  const [dragActive, setDragActive] = useState(false); // 拖拽悬停高亮
  const [uploadError, setUploadError] = useState(''); // 文件预检错误（超限/超量）
  const micErrorTimerRef = useRef(null);
  const fileInputRef = useRef(null);
  const textareaRef = useRef(null);
  const voiceRecorderRef = useRef(null);
  const voiceBaseRef = useRef('');  // 语音录音开始前输入框已有文本（追加模式基准）
  const sendingRef = useRef(false);   // 发送锁：防双击/重复点击重复提交

  // textarea 自动增高（2026-09-01 起：单行 input → 多行 textarea，粘贴多行文本保真换行，
  // 输入框随内容增高，最多 ~6 行后内部滚动）
  const autoGrow = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 6 * 24 + 16)}px`;
  };
  useEffect(() => {
    autoGrow();
  }, [text]);

  useEffect(() => {
    return () => clearTimeout(micErrorTimerRef.current);
  }, []);

  const showMicError = (msg) => {
    setMicError(msg);
    clearTimeout(micErrorTimerRef.current);
    micErrorTimerRef.current = setTimeout(() => setMicError(''), 3000);
  };

  const addFiles = (list) => {
    const incoming = Array.from(list);
    if (!incoming.length) return;
    setUploadError('');
    const oversize = incoming.filter((f) => f.size > MAX_FILE_SIZE);
    if (oversize.length) {
      setUploadError(`「${oversize[0].name}」超过 10MB 限制`);
      incoming.splice(0, incoming.length, ...incoming.filter((f) => f.size <= MAX_FILE_SIZE));
    }
    if (files.length + incoming.length > MAX_FILES) {
      setUploadError(`一次最多发送 ${MAX_FILES} 个文件`);
      return;
    }
    if (!incoming.length) return;
    setFiles((prev) => [
      ...prev,
      ...incoming.map((f) => ({ file: f, name: f.name, size: f.size })),
    ]);
  };

  const removeFile = (idx) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleSend = async () => {
    // 发送锁：上一次发送完成前忽略重复点击（Enter 双击 / 快速连点发送按钮）
    if (sendingRef.current) return;
    sendingRef.current = true;
    try {
      let content = text.trim();
      // 录音中点击发送：先停止录音并拿最终语音文本再发送（一次点击 = 关麦 + 发送，
      // 避免 pointerdown 自动停止与 click 发送之间的文本刷新异步时序导致点两次）
      if (listening && voiceRecorderRef.current?.stopAndGetText) {
        const voiceText = await voiceRecorderRef.current.stopAndGetText();
        // 追加模式（2026-08-24）：语音转写是"输入框已有内容之外"的新文本，
        // 完整发送内容 = 录音前已有文本 + 本次语音转写全文（不可用 text state——
        // await 期间 React setState 尚未提交，读到的 text 是旧值）
        const full = `${voiceBaseRef.current}${voiceText}`.trim();
        content = full || content;
      }
      if (!content && files.length === 0) return;
      await onSend(content, files.map((f) => f.file));
      setText('');
      setFiles([]);
      // 语音文本已发送：通知 VoiceRecorder 阻断迟到的 emitText（onend/onstop 延迟回调
      // 会把语音文字写回输入框，导致"发送了但输入框文字还在"，需手动删除，2026-08-23 修）
      voiceRecorderRef.current?.markTextSent?.();
    } finally {
      sendingRef.current = false;
    }
  };

  // 录音中即使文本暂空也允许点发送（"停止并发送当前语音"；识别为空则不发送）
  const canSend = listening || !!text.trim() || files.length > 0;

  return (
    <div className="border-t border-slate-200 bg-white">
      {micError && (
        <div className="px-4 pt-2 text-xs text-red-500">{micError}</div>
      )}
      {uploadError && (
        <div className="px-4 pt-2 text-xs text-red-500">{uploadError}</div>
      )}
      {/* 已选文件 chips：显示文件名/大小，可删除；与文字输入并存（12 §2） */}
      {files.length > 0 && (
        <div className="flex flex-wrap gap-2 px-4 pt-3">
          {files.map((f, i) => (
            <span
              key={`${f.name}_${i}`}
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-indigo-50 border border-indigo-200 text-xs text-indigo-700"
            >
              <svg className="w-3.5 h-3.5 shrink-0" viewBox="0 0 20 20" fill="currentColor">
                <path d="M4 3a2 2 0 012-2h4.586A2 2 0 0112 1.586L16.414 6A2 2 0 0117 7.414V17a2 2 0 01-2 2H6a2 2 0 01-2-2V3z" />
              </svg>
              <span className="max-w-[12rem] truncate">{f.name}</span>
              <span className="text-indigo-400">{formatSize(f.size)}</span>
              <button
                onClick={() => removeFile(i)}
                title="删除该文件"
                className="ml-0.5 text-indigo-400 hover:text-red-500"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragActive(true); }}
        onDragLeave={() => setDragActive(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragActive(false);
          addFiles(e.dataTransfer.files);
        }}
        className={`flex items-center gap-2 p-4 transition ${
          dragActive ? 'bg-indigo-50/60 ring-2 ring-indigo-300 rounded-xl mx-2 mb-2' : ''
        }`}
      >
        <VoiceRecorder
          ref={voiceRecorderRef}
          // 追加模式（2026-08-24）：录音开始记录输入框已有文本作基准，转写结果拼在其后，
          // 支持"手动输入 + 多次语音累加"；发送后 setText('') 全清
          onRecordingChange={(v) => {
            setListening(v);
            if (v) voiceBaseRef.current = text;
          }}
          onInterim={(t) => setText(voiceBaseRef.current + t)}
          onError={showMicError}
        />
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => { addFiles(e.target.files); e.target.value = ''; }}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          title="上传文件/图片（可拖拽到此处）"
          className="rounded-xl px-2.5 py-2 text-sm border border-slate-300 text-slate-600 bg-white hover:bg-slate-50 shrink-0"
        >
          <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
            <path d="M4 3a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V5a2 2 0 00-2-2H4zm5 4a1 1 0 112 0v2h2a1 1 0 110 2h-2v2a1 1 0 11-2 0V11H7a1 1 0 110-2h2V7z" />
          </svg>
        </button>
        <textarea
          ref={textareaRef}
          rows={1}
          className={`flex-1 border rounded-xl px-3 py-2 text-sm outline-none transition focus:ring-2 resize-none overflow-hidden leading-6 ${
            listening
              ? 'border-red-400 focus:ring-red-100 focus:border-red-400'
              : 'border-slate-300 focus:ring-indigo-200 focus:border-indigo-400'
          }`}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onInput={autoGrow}
          onKeyDown={(e) => {
            // Enter 发送、Shift+Enter 换行（2026-09-01 起：textarea 多行输入，
            // 粘贴多行文本保留换行，不再被单行 input 吞掉）
            // IME 组合态（中文输入法选词中）按 Enter 不应发送（e.nativeEvent.isComposing 是标准属性）
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder={
            listening
              ? '正在聆听，说出你的问题…'
              : '输入消息，Enter 发送，Shift+Enter 换行；可拖拽图片/文档到此处'
          }
        />
        <button
          onClick={handleSend}
          disabled={!canSend}
          className={`px-4 py-2 rounded-xl text-sm font-medium transition ${
            canSend
              ? 'bg-gradient-to-r from-indigo-500 to-violet-500 text-white hover:from-indigo-600 hover:to-violet-600'
              : 'bg-slate-200 text-slate-400 cursor-not-allowed'
          }`}
        >
          发送
        </button>
      </div>
    </div>
  );
}