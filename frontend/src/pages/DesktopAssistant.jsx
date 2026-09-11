import { useEffect, useRef, useState } from 'react';
import { voiceApi } from '../api/voice';
import { sessionsApi } from '../api/sessions';
import { streamChat } from '../api/chat';
import { useModelStatus } from '../hooks/useModelStatus';
import { ChatMessage } from '../components/ChatMessage';
import { stripMarkdown } from '../utils/text';
import { blobToWav16k } from '../utils/audio';

// 桌面助手固定会话键（08：全部问答归档到同一会话，避免碎片化）
const DESKTOP_SESSION_KEY = 'desktop_session_id';

// 段级静音判定：低于 offTh 持续约 1.6 秒切一段录音并转写
//（拆分由合并窗口 MERGE_WINDOW_MS 愈合；2026-08-16 晚由 1.2s 提到 1.6s——给中文口语
// 换气/想词的常见停顿留出余量，减少"一句话被拆成多段"的触发）
const SILENCE_MS = 1600;
// 最小有效语音时长：低于此视为环境噪声/鼠标声误触发，丢弃不转写
const MIN_VOICE_MS = 300;
// 打断判定：音量超阈值帧长累积达到此值才打断（自然说话的字间隙会半速衰减但不清零，不会误触发）
const INTERRUPT_CONFIRM_MS = 200;
// 合并窗口：用户停止说话 2.5 秒后，把之前累积的所有句子作为一轮统一发送（避免一句话拆多个回复）
const MERGE_WINDOW_MS = 2500;
// 合并缓冲长度上限：超过立即 flush，防无限累积
const MAX_PENDING_LEN = 500;

// 句边界：最后一个完整句子结束的字符索引（。！？!?…\n 等分隔），无完整句返回 -1。
// 用于句级 TTS：回复流式到达时按句切分，完整句子尽早入队播放（首句提前合成，不等整段流完）
const lastSentenceBoundary = (text) => {
  let last = -1;
  for (let i = 0; i < text.length; i++) {
    if ('。！？!?…\n'.includes(text[i])) last = i + 1;
  }
  return last;
};

// 错误码 → 友好文案（桌面助手）：后端错误帧通常已是中文友好句，这里只映射特殊码
const friendlyError = (raw, code) => {
  if (code === 'chat_model_not_configured') return '您还没有配置对话模型，请到设置页配置';
  if (code === 'network_error') return '网络异常，请确认后端服务已启动';
  return raw || '未知错误，请重试';
};

let turnSeq = 0; // 转录面板消息 id（本地递增，无需持久化）

export default function DesktopAssistant() {
  const [isListening, setIsListening] = useState(false);
  const [status, setStatus] = useState('等待中...');
  const [volume, setVolume] = useState(0);          // 实时音量（VAD level 节流渲染音量条，0~100）
  const [textInput, setTextInput] = useState('');   // 文字输入兜底：麦克风/识别故障时仍可对话
  const [micError, setMicError] = useState('');     // 麦克风启动失败原因（权限被拒/无设备/被占用）
  const [turns, setTurns] = useState([]);        // 转录面板：每轮用户话 + 思考 + Markdown 回复
  const [streamingId, setStreamingId] = useState(null); // 正在流式输出的 assistant turn id
  const [voiceMuted, setVoiceMuted] = useState(false);  // 语音回复开关：静音时文字照常渲染/落库，仅不播 TTS
  const [lastReply, setLastReply] = useState('');       // 最近一条回复纯文本（显示/启用「重播上一条」按钮）
  const { chat_configured: chatConfigured } = useModelStatus();
  // onstop → processVoice 是首次渲染建立的闭包，须用 ref 读取最新配置状态
  const chatConfiguredRef = useRef(false);
  useEffect(() => { chatConfiguredRef.current = chatConfigured; }, [chatConfigured]);
  const voiceMutedRef = useRef(false);   // 静音开关的 ref 镜像（VAD/播放闭包中读取最新值）
  useEffect(() => { voiceMutedRef.current = voiceMuted; }, [voiceMuted]);
  // 转录面板自动滚动：turns/流式状态变化时始终跟随最新消息，无需手动翻
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, streamingId]);
  const abortRef = useRef(null);       // 当前对话的 AbortController（打断时中断流式请求）
  const mediaRecorderRef = useRef(null);
  const chunksRef = useRef([]);
  const silenceTimerRef = useRef(null);
  const audioContextRef = useRef(null);
  const sessionIdRef = useRef(null);
  const suppressRef = useRef(false);   // TTS 播放期间屏蔽 VAD，避免把回复识别成输入
  const audioRef = useRef(null);       // 当前播放的回复音频（停止监听/打断时中断）
  const streamRef = useRef(null);      // getUserMedia 的 stream（单独保存，停麦第一时间停 tracks）
  const rafIdRef = useRef(null);       // VAD requestAnimationFrame id（卸载/停止时取消）
  const aliveRef = useRef(true);       // 组件存活标志（卸载后不再播放 TTS，但对话落库不受影响）
  const genRef = useRef(0);            // 对话代际计数器：打断时 +1，令在途 processVoice 过期
  const startGenRef = useRef(0);       // 麦克风启动代际计数器：防异步 start 覆盖 stop/卸载后泄漏流
  const busyRef = useRef(false);       // true=思考中或播放回复中（此时检测到用户语音即打断）
  const recordingRef = useRef(false);  // VAD 是否正在录音本段（与 recorder.state 同步，供噪声校准/静音判定）
  const recStartTimeRef = useRef(0);   // 本段录音开始时刻（performance.now()），用于最小语音时长判定
  const interruptAccumRef = useRef(0);        // 打断累积量：超阈值帧按帧长累加、低于阈值半速衰减，达 INTERRUPT_CONFIRM_MS 才打断
  const belowSinceRef = useRef(0);            // 低于打断阈值持续起点（performance.now()），用于瞬态段丢弃判定
  const suppressStartRef = useRef(0);         // TTS 开始播放时刻（预热期只校准 baseAvg 不判定打断）
  const armedRef = useRef(false);             // 打断已开录但未确认（瞬态噪声掉回阈值下则丢弃该段）
  const dropNextRef = useRef(false);          // 丢弃下一段录音（打断未确认的瞬态噪声段）
  const pendingTextRef = useRef('');          // 合并缓冲：当前轮累积的语音文本（停顿 MERGE_WINDOW_MS 后统一发送）
  const finalizeTimerRef = useRef(null);      // 合并窗口定时器：用户停止说话 MERGE_WINDOW_MS 后 flush 一轮
  const activeUserTurnIdRef = useRef(null);   // 当前正在累积的用户消息 turn id（转写逐段实时上屏，flush 时封口）
  const lastReplyRef = useRef('');            // 最近一条回复纯文本（「重播上一条」按钮用，与 lastReply state 同步）
  const transcriptRef = useRef(null);  // 转录面板容器 DOM（自动滚动跟随最新）
  const textAreaRef = useRef(null);    // 文字输入 textarea（2026-09-01 起：单行 input → 多行 textarea 保真换行）
  const gestureResumeRef = useRef(null); // AudioContext 用户激活兜底的一次性监听器（stop/卸载时移除）
  const thinkingPendingRef = useRef(''); // 思考打字机：待显示的剩余思考内容
  const thinkingTimerRef = useRef(null); // 思考打字机：逐字 reveal 定时器（interrupt/卸载时清理）
  const pageFocusedRef = useRef(true);        // 页面聚焦标志：blur 时置 false，抑制 TTS 出声（文字照常落库）
  const speechQueueRef = useRef([]);          // 句级 TTS 待播句子队列（首句提前合成播放，边流式边出声）
  const speechCursorRef = useRef(0);          // 已入队 TTS 的 assistantText 字符位置
  const speechPlayingRef = useRef(false);     // 句级 TTS 是否正在播放（串行逐句播放）
  const replyFinishedRef = useRef(false);     // 本轮流式是否已结束（finish 调用过，用于句队列收尾）
  const lastVolumeSentRef = useRef(0);        // 音量条 setState 节流（避免每帧 setState）
  const assistantTextRef = useRef('');        // 本轮已流式到达的回复文字（打断时固定到面板，不丢失）
  const thinkingTextRef = useRef('');         // 本轮思考全文（已揭晓+待揭晓；打断时固定，不丢弃）

  // 麦克风启动失败 → 面向用户的原因描述（地址栏锁图标/设备/占用三类）
  const describeMicError = (e) => {
    const name = e?.name || '';
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return '麦克风权限被拒，请点击浏览器地址栏左侧的锁图标，允许使用麦克风后重试';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return '未检测到麦克风设备，请检查是否已插入/开启';
    }
    if (name === 'NotReadableError' || name === 'TrackStartError') {
      return '麦克风被其他应用占用，请关闭占用程序后重试';
    }
    return `麦克风启动失败：${e?.message || name || '未知错误'}`;
  };

  // 初始化固定会话：localStorage 复用已有 id，无则创建 source=desktop 会话；
  // 已有会话时顺带把最近消息加载进转录面板（打开页面就能看到之前的对话，而非空白）
  useEffect(() => {
    const cached = localStorage.getItem(DESKTOP_SESSION_KEY);
    if (cached) {
      sessionIdRef.current = cached;
      sessionsApi
        .get(cached)
        .then((s) => {
          const msgs = (s?.messages || [])
            .slice()
            .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
          if (!msgs.length) return;
          setTurns(msgs.slice(-40).map((m) => ({
            id: `hist_${m.id}`,
            role: m.role,
            content: m.content || '',
            thinking: m.thinking || '',
          })));
        })
        .catch((e) => console.error('load desktop history failed:', e));
      return;
    }
    sessionsApi
      .create('desktop')
      .then((s) => {
        sessionIdRef.current = s.id;
        localStorage.setItem(DESKTOP_SESSION_KEY, s.id);
      })
      .catch((e) => console.error('create desktop session failed:', e));
  }, []);

  // 纯自动监听：打开页面即开始，切换路由/离开页面（组件卸载）自动停止，无手动按钮
  useEffect(() => {
    aliveRef.current = true;
    startListening();
    return () => {
      aliveRef.current = false;
      // 卸载时中断在途 SSE 流，防卸载后 setState-on-unmounted（2026-08-17 修）
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
      stopListening();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 页面失焦自动关闭：切到别的浏览器标签页/最小化/失焦时立即停止监听并释放麦克风；
  // 回到页面且组件仍挂载时自动恢复监听（start/stop 幂等，防重复启动）。
  // blur（点地址栏/其他窗口/开发者工具）不伴随 hidden 切换，须用 focus 事件补齐恢复。
  useEffect(() => {
    const handleVisibility = () => {
      if (!aliveRef.current) return;
      if (document.hidden) {
        pageFocusedRef.current = false;
        stopListening();
      } else {
        pageFocusedRef.current = true;
        startListening();
      }
    };
    const handleBlur = () => {
      pageFocusedRef.current = false;
      if (aliveRef.current) stopListening();
    };
    const handleFocus = () => {
      pageFocusedRef.current = true;
      if (aliveRef.current && !document.hidden) startListening();
    };
    document.addEventListener('visibilitychange', handleVisibility);
    window.addEventListener('blur', handleBlur);
    window.addEventListener('focus', handleFocus);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility);
      window.removeEventListener('blur', handleBlur);
      window.removeEventListener('focus', handleFocus);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startListening = async () => {
    if (streamRef.current) return;   // 幂等：已在监听时不重复启动（blur/focus 交错触发的保护）
    const gen = ++startGenRef.current;   // 记录本次启动代际，stop/卸载后本代际即失效
    if (!navigator.mediaDevices?.getUserMedia) {
      const msg = '当前环境不支持麦克风，请用 localhost 或 HTTPS 访问';
      setStatus(msg);
      setMicError(msg);
      setIsListening(false);
      return;
    }
    let stream;
    try {
      // 开启 AEC/降噪/自动增益：TTS 回复声被麦克风拾回是"自打断/回声转写"的源头，
      // echoCancellation 让浏览器消除自己扬声器的回声（部分设备/浏览器不支持则降级重试）
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (e) {
        if (gen !== startGenRef.current || !aliveRef.current) return;
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      }
    } catch (e) {
      // await 挂起期间可能已被 stopListening（blur/切页）或组件卸载：忽略过期错误
      if (gen !== startGenRef.current || !aliveRef.current) return;
      const msg = describeMicError(e);
      setStatus(msg);
      setMicError(msg);
      setIsListening(false);
      return;
    }
    // 异步期间被 stop/卸载：立即释放刚拿到的流，防止 start 覆盖 stop 状态并泄漏麦克风
    if (gen !== startGenRef.current || !aliveRef.current) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    streamRef.current = stream;
    try {
      audioContextRef.current = new AudioContext();
      // 自动播放策略：无用户手势时 AudioContext 为 suspended，AnalyserNode 无数据、
      // VAD 音量恒为 0 → 永远检测不到人声（"听不到说话"的根因）。启动即 resume，
      // VAD 循环内另有兜底自动恢复（见 setupVAD）。
      if (audioContextRef.current.state === 'suspended') {
        try { await audioContextRef.current.resume(); } catch (e) { /* noop */ }
      }
      // 首次授权时 getUserMedia 的权限弹窗可能耗尽浏览器"用户激活"窗口（约 5s），
      // resume 会静默失败（context 仍 suspended）。注册一次性点击/按键监听，
      // 用户下次点击页面时 resume 并自移除（VAD 每帧 resume 兜底仍在）。
      if (audioContextRef.current.state !== 'running') {
        gestureResumeRef.current = () => {
          try { audioContextRef.current?.resume(); } catch (e) { /* noop */ }
          document.removeEventListener('pointerdown', gestureResumeRef.current);
          document.removeEventListener('keydown', gestureResumeRef.current);
          gestureResumeRef.current = null;
        };
        document.addEventListener('pointerdown', gestureResumeRef.current);
        document.addEventListener('keydown', gestureResumeRef.current);
      }

      const source = audioContextRef.current.createMediaStreamSource(stream);
      const recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => chunksRef.current.push(e.data);
      recorder.onstop = async () => {
        // 代际守卫（2026-08-24 修）：stopListening 不再提前清 chunksRef，由 onstop 收尾；
        // 但若已开始新录音（新 recorder 接管了共享的 chunksRef），旧 recorder 的收尾
        // 不得处理或清空 chunksRef——否则会误转写/清掉新录音的分片
        if (mediaRecorderRef.current && mediaRecorderRef.current !== recorder) {
          return;
        }
        // 打断未确认的瞬态噪声段（如鼠标点击）直接丢弃，不转写、不打扰正常流程
        if (dropNextRef.current) {
          dropNextRef.current = false;
          chunksRef.current = [];
          return;
        }
        const blob = new Blob(chunksRef.current, { type: 'audio/webm' });
        chunksRef.current = [];
        // 前端解码为 16kHz 单声道 WAV 再转写（绕开 ffmpeg 7.x 解析 Chrome webm 报 EOF）
        try {
          const wav = await blobToWav16k(blob);
          if (wav) await processVoice(wav);
        } catch (e) {
          console.error('blobToWav16k failed:', e);
        }
      };
      // 必须先赋值 mediaRecorderRef 再启动 VAD：setupVAD 首帧同步执行 checkVolume，
      // 若 recorder 尚未就绪会访问 null.state 抛 TypeError，导致整个监听启动崩溃
      //（此前"一直已停止监听、不对话不输出"的根因）。
      mediaRecorderRef.current = recorder;
      setupVAD(source);
    } catch (e) {
      // 初始化失败（AudioContext / MediaRecorder / VAD 任一步）：释放流并提示，
      // 由「重新启动监听」按钮恢复，不留静默死状态
      stream.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      try { audioContextRef.current?.close(); } catch (e2) { /* noop */ }
      audioContextRef.current = null;
      mediaRecorderRef.current = null;
      const msg = `音频初始化失败：${e?.message || '未知错误'}`;
      setStatus(msg);
      setMicError(msg);
      setIsListening(false);
      return;
    }
    setMicError('');
    setIsListening(true);
    setVolume(0);
    setStatus('正在监听...');
    // 切页回来：若合并缓冲还有未发送内容，重新挂上合并窗口（不丢已听句子）
    if (pendingTextRef.current && !finalizeTimerRef.current) {
      finalizeTimerRef.current = setTimeout(flushPendingTurn, MERGE_WINDOW_MS);
    }
  };

  // 打断当前思考/回复：用户开口时调用，中断在途流式请求、停止 TTS、令在途 processVoice 过期，转听新语音
  const interrupt = () => {
    genRef.current += 1;              // 令在途 processVoice 过期（已发后端的对话照常落库）
    abortRef.current?.abort();        // 中断流式生成（SSE 连接断开）
    abortRef.current = null;
    // 停止思考打字机定时器；把剩余未揭晓的思考并入累积，不丢弃（打断后思路保留在面板）
    if (thinkingTimerRef.current) {
      clearInterval(thinkingTimerRef.current);
      thinkingTimerRef.current = null;
    }
    if (thinkingPendingRef.current) {
      thinkingTextRef.current += thinkingPendingRef.current;
      thinkingPendingRef.current = '';
    }
    busyRef.current = false;
    suppressRef.current = false;      // 恢复 VAD 正常处理（不再屏蔽音量）
    setStreamingId(null);
    if (audioRef.current) {
      audioRef.current.pause();       // 立即停止播放中的回复
      audioRef.current = null;
    }
    clearTimeout(silenceTimerRef.current);
    clearTimeout(finalizeTimerRef.current);   // 打断后进入新一轮：取消旧的合并窗口
    finalizeTimerRef.current = null;
    // 句级 TTS 播放链终止：清队列 + 复位在播标志（audioRef 已 pause，onended 不会触发）
    speechQueueRef.current = [];
    speechPlayingRef.current = false;
    // 若合并缓冲还有未发送内容，重新挂合并窗口（防打断后句子永久滞留不发送）
    if (pendingTextRef.current) {
      finalizeTimerRef.current = setTimeout(flushPendingTurn, MERGE_WINDOW_MS);
    }
    armedRef.current = false;               // 已确认打断：当前录音段正常继续录，不再丢弃
    interruptAccumRef.current = 0;
    belowSinceRef.current = 0;
    suppressStartRef.current = 0;
    // 若尚未开始录音则立即开始录新语音；正在录音则保持（VAD 正常流程继续处理）
    if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'inactive') {
      try { mediaRecorderRef.current.start(); } catch (e) { /* noop */ }
      recordingRef.current = true;
      recStartTimeRef.current = performance.now();
    }
    // 固定被中断一轮的最终状态：已生成的思考与文字保留并标记「（已打断）」——
    // 此前打断会把未揭晓的思考清空、空白占位 turn 整条删除，用户补话时"第一句回答就没了"。
    // 仅当完全空白（尚未产生任何思考/文字）的占位 turn 才删除（不留空气泡）。
    setTurns((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      if (!last.content && !last.thinking) {
        return prev.slice(0, -1);
      }
      return [...prev.slice(0, -1), {
        ...last,
        content: assistantTextRef.current || last.content,
        thinking: thinkingTextRef.current || last.thinking,
        interrupted: true,
      }];
    });
    setStatus('听到你了，请说...');
  };

  const setupVAD = (source) => {
    // 生产请替换为 webrtcvad（@ricky0123/vad-react）+ 静音检测 + 回声消除；
    // 下方为基于 AnalyserNode 的兜底静音检测示例（04 §10 / 08 §2.3）。
    // 2026-08-15 起用时间域 RMS + 自适应噪声底（替代频域平均）：
    //   - 旧实现用 new Uint8Array(analyser.fftSize) + getByteFrequencyData，
    //     但频域数据只填前 frequencyBinCount=fftSize/2 个元素，后一半恒 0，
    //     音量平均被稀释一半，阈值 30 实际等效 60 → 人声很难触发（"监听中但不响应"根因）。
    //   - 时间域 getFloatTimeDomainData 填满整个数组，RMS 能量更准更灵敏。
    // 2026-08-16 起 VAD 体验重构（见当天台账）：
    //   - 噪声底改 EMA + clamp（替代 Math.min 单调递减）：非语音帧才校准，环境变化能跟上，
    //     不被 TTS 回声/环境声一次性污染锁高 → 修复"一句话被拆/没说完就中断"；
    //   - 开录/停录分离迟滞阈值 onTh/offTh，消除阈值附近抖动导致 recorder 反复 start/stop 的碎片段；
    //   - 打断"确认即开录"：检测到超阈值第一帧就 recorder.start()（不丢用户开头），
    //     300ms 确认后才真正 interrupt()，瞬态噪声掉回阈值下则丢弃该段；
    //   - TTS 播放中打断改"突发能量比"：跟踪含回声的基础能量 baseAvg，用户说话相对 TTS 是突发
    //     能量才打断，替代固定 0.08 下限（用户说话音量常达不到 0.08 → "打断不了"）。
    const analyser = audioContextRef.current.createAnalyser();
    analyser.fftSize = 1024;                    // 1024 点足够，降低计算量
    analyser.smoothingTimeConstant = 0.3;       // 降低平滑，响应更快
    source.connect(analyser);
    const timeData = new Float32Array(analyser.fftSize);
    let noiseFloor = 0.01;                      // 自适应噪声底（EMA + clamp，初始合理值）
    let baseAvg = 0;                            // TTS 播放中的基础能量（慢 EMA，含回复回声）
    let lastFrameAt = 0;                        // 上一帧 rAF 时间戳（打断累积按实际帧长计量）

    const rms = () => {
      analyser.getFloatTimeDomainData(timeData);
      let sum = 0;
      for (let i = 0; i < timeData.length; i++) sum += timeData[i] * timeData[i];
      return Math.sqrt(sum / timeData.length);
    };

    const checkVolume = (ts) => {
      // 兜底恢复：某些浏览器手势丢失后 AudioContext 会被再次挂起，VAD 前先 resume
      if (audioContextRef.current && audioContextRef.current.state === 'suspended') {
        try { audioContextRef.current.resume(); } catch (e) { /* noop */ }
      }
      // 竞态防护：recorder 尚未就绪（理论不应发生，防 stop/卸载清空引用后 VAD 崩溃）
      if (!mediaRecorderRef.current) {
        rafIdRef.current = requestAnimationFrame(checkVolume);
        return;
      }
      const level = rms();
      // 帧间隔实际时长（rAF 时间戳差值）：打断累积按真实毫秒计量，
      // 120Hz/144Hz 屏上不再把每帧当 16ms 累加（否则确认窗 200ms 实际约 90~120ms 就触发）
      const nowTs = ts || performance.now();
      const frameMs = lastFrameAt ? Math.min(50, nowTs - lastFrameAt) : 16.7;
      lastFrameAt = nowTs;
      // 音量条节流渲染（每 150ms 一次 setState，避免每帧重渲染整个组件）
      if (nowTs - lastVolumeSentRef.current > 150) {
        lastVolumeSentRef.current = nowTs;
        setVolume(Math.min(100, Math.round(level * 250)));
      }

      // ---- TTS 播放中（suppress）：用户说话相对回复声是突发能量才打断 ----
      if (suppressRef.current) {
        // TTS 预热期（前 300ms）：只校准 baseAvg、不判定打断。回复起播瞬间麦克风会拾回大量
        // 开头回声（无 AEC），若照常累加打断量会在约 0.3s 时累积满 200 触发 interrupt()，
        // 导致助手每播一句话就把自己打断、回声段又被当成"用户说话"转写 → 无限循环/胡言乱语。
        // 预热期每帧都做 baseAvg EMA（TTS 开头回声正是要锁进基线的样本），预热结束才判定。
        const inWarmup = suppressStartRef.current
          && (performance.now() - suppressStartRef.current <= 300);
        if (inWarmup) {
          baseAvg = baseAvg * 0.92 + level * 0.08;
          rafIdRef.current = requestAnimationFrame(checkVolume);
          return;
        }
        const burstTh = baseAvg + Math.max(0.02, baseAvg * 0.5);
        if (level >= burstTh) {
          // 超阈值帧：累积打断量（用户声音叠在回复声上，相对基线增量明显）
          interruptAccumRef.current += frameMs;
          belowSinceRef.current = 0;
          // 确认即开录：第一帧就开录，用户开头不丢
          if (mediaRecorderRef.current.state === 'inactive') {
            try { mediaRecorderRef.current.start(); } catch (e) { /* noop */ }
            recordingRef.current = true;
            recStartTimeRef.current = performance.now();
            armedRef.current = true;
          }
          if (interruptAccumRef.current >= INTERRUPT_CONFIRM_MS) interrupt();
        } else {
          // 非语音帧：更新基础能量（含 TTS 回声），打断累积半速衰减（自然说话的字间隙不清零）
          baseAvg = baseAvg * 0.92 + level * 0.08;
          interruptAccumRef.current = Math.max(0, interruptAccumRef.current - frameMs / 2);
          // 低于阈值持续超 400ms 且累积已清零 → 视为瞬态噪声段（鼠标点击等），丢弃
          if (belowSinceRef.current) {
            if (performance.now() - belowSinceRef.current > 400 && interruptAccumRef.current === 0) {
              if (armedRef.current) {
                armedRef.current = false;
                dropNextRef.current = true;
                try {
                  if (mediaRecorderRef.current.state === 'recording') mediaRecorderRef.current.stop();
                } catch (e) { /* noop */ }
                recordingRef.current = false;
              }
            }
          } else {
            belowSinceRef.current = performance.now();
          }
        }
        rafIdRef.current = requestAnimationFrame(checkVolume);
        return;
      }

      // ---- 思考中（busy）：低阈值打断 + 累积判定 ----
      if (busyRef.current) {
        const interruptThreshold = Math.min(0.08, Math.max(0.03, noiseFloor * 4));
        if (level > interruptThreshold) {
          interruptAccumRef.current += frameMs;
          belowSinceRef.current = 0;
          // 确认即开录：第一帧就开录，用户开头不丢
          if (mediaRecorderRef.current.state === 'inactive') {
            try { mediaRecorderRef.current.start(); } catch (e) { /* noop */ }
            recordingRef.current = true;
            recStartTimeRef.current = performance.now();
            armedRef.current = true;
          }
          if (interruptAccumRef.current >= INTERRUPT_CONFIRM_MS) interrupt();
        } else {
          interruptAccumRef.current = Math.max(0, interruptAccumRef.current - frameMs / 2);
          // 低于阈值持续超 400ms 且累积已清零 → 视为瞬态噪声段，丢弃
          if (belowSinceRef.current) {
            if (performance.now() - belowSinceRef.current > 400 && interruptAccumRef.current === 0) {
              if (armedRef.current) {
                armedRef.current = false;
                dropNextRef.current = true;
                try {
                  if (mediaRecorderRef.current.state === 'recording') mediaRecorderRef.current.stop();
                } catch (e) { /* noop */ }
                recordingRef.current = false;
              }
            }
          } else {
            belowSinceRef.current = performance.now();
          }
        }
        rafIdRef.current = requestAnimationFrame(checkVolume);
        return;
      }

      // ---- 正常监听 / 录音：迟滞阈值 + EMA 噪声校准 ----
      const onTh = Math.min(0.06, Math.max(0.02, noiseFloor * 3));
      const offTh = onTh * 0.7;
      // 噪声校准（EMA + clamp）：空闲非语音帧快跟随环境；录音中静音帧慢校准防锁高
      if (level < onTh && !recordingRef.current) {
        noiseFloor = Math.min(0.03, Math.max(0.005, noiseFloor * 0.95 + level * 0.05));
      } else if (recordingRef.current && level < offTh) {
        noiseFloor = Math.min(0.03, Math.max(0.005, noiseFloor * 0.97 + level * 0.03));
      }

      if (level > onTh) {
        if (mediaRecorderRef.current.state === 'inactive') {
          mediaRecorderRef.current.start();
          recordingRef.current = true;
          recStartTimeRef.current = performance.now();   // 记录本段开始，用于最小语音时长判定
          setStatus('听到你了，请说...');
        }
        // 有声音：取消静音定时器。必须先 clearTimeout 再（不）新建，
        // 否则低音量帧每帧 new setTimeout 会堆叠出几十个"幽灵定时器"，
        // 在用户换气/短暂停顿时提前到点掐断正在说话的录音（一句话被拆几段的根因）。
        if (silenceTimerRef.current) {
          clearTimeout(silenceTimerRef.current);
          silenceTimerRef.current = null;
        }
      } else if (mediaRecorderRef.current.state === 'recording' && level < offTh) {
        // 低于 offTh 持续 SILENCE_MS 才停止录音。offTh 停录迟滞：offTh~onTh 中间区段
        // 保持录音，消除阈值附近音量抖动导致 recorder 反复 start/stop 的碎片段。
        // 仅在没有定时器时新建一个，回调内再校验仍处于录音态（防 onTh 帧 clear 后旧回调已入队）。
        if (!silenceTimerRef.current) {
          silenceTimerRef.current = setTimeout(() => {
            silenceTimerRef.current = null;
            if (!recordingRef.current || mediaRecorderRef.current.state !== 'recording') return;
            // 语音时长过短（如环境噪声/鼠标声）→ 丢弃不转写，静默恢复监听
            if (performance.now() - recStartTimeRef.current < MIN_VOICE_MS) {
              recordingRef.current = false;
              try { mediaRecorderRef.current.stop(); } catch (e) { /* noop */ }
              return;
            }
            try { mediaRecorderRef.current.stop(); } catch (e) { /* noop */ }
            recordingRef.current = false;
            setStatus('识别中...');
          }, SILENCE_MS);
        }
      }
      // 合并窗口取消：只要在录音中或检测到声音，就不能让窗口到点 flush。
      // 此前只在 level>onTh 分支 clear，用户轻声说话（level 落在 offTh~onTh 中间区）时
      // 窗口继续计时，到点把未说完的句子截断发送（2026-08-16 晚审查发现）。
      if (recordingRef.current || level > onTh) {
        if (finalizeTimerRef.current) {
          clearTimeout(finalizeTimerRef.current);
          finalizeTimerRef.current = null;
        }
      }
      rafIdRef.current = requestAnimationFrame(checkVolume);
    };
    checkVolume();
  };

  // 合并窗口到期：把累积的所有句子作为一轮统一发送（用户停顿确认说完，避免一句话拆多个回复）
  const flushPendingTurn = () => {
    if (finalizeTimerRef.current) {
      clearTimeout(finalizeTimerRef.current);
      finalizeTimerRef.current = null;
    }
    // 注意：activeUserTurnIdRef 的封口统一交给 sendTurn 处理（语音/文字两条路径都要封口）
    const text = pendingTextRef.current.trim();
    pendingTextRef.current = '';
    if (!text) return;
    sendTurn(text);
  };

  // 播放回复（TTS）：先 stripMarkdown 清洗 markdown 符号，再请求合成并播放。
  // 播放期间 suppressRef=true 屏蔽 VAD（回复回声不触发打断，用户开口才是突发能量）；
  // 静音开关打开时跳过合成与播放（文字已在转录面板渲染并落库）。
  // gen 为本轮对话代际：被打断/卸载后不播放旧回复（已落库，仅释放音频资源）。
  const playReply = async (text, gen) => {
    const clean = stripMarkdown(text);
    // 页面失焦/隐藏时不播声音（重播按钮点击时页面必已聚焦，不受影响；语音回复在
    // 切页后只落库不发声——2026-08-16 晚审查发现：blur 触发 stopListening 的尾段录音
    // 仍会走到这里，此前会在用户切到别的标签页时突然冒出回复声）
    if (!pageFocusedRef.current) {
      busyRef.current = false;
      suppressRef.current = false;
      suppressStartRef.current = 0;
      setStatus('正在监听...');
      return;
    }
    if (!clean) {
      busyRef.current = false;
      suppressRef.current = false;
      suppressStartRef.current = 0;
      setStatus('正在监听...');
      return;
    }
    try {
      if (voiceMutedRef.current) {
        busyRef.current = false;
        suppressRef.current = false;
        suppressStartRef.current = 0;
        setStatus('正在监听...');
        return;
      }
      const audioUrl = await voiceApi.tts(clean);
      if (gen !== genRef.current || !aliveRef.current) {
        // 已被打断或页面已卸载：不播放声音（对话已通过后端落库），释放音频
        URL.revokeObjectURL(audioUrl);
        busyRef.current = false;
        suppressRef.current = false;
        return;
      }
      setStatus('播放回复中...');   // 播放期间仍监听，用户说话即打断
      suppressRef.current = true;
      suppressStartRef.current = performance.now();   // 预热期起点：只校准 baseAvg 不判定打断
      // 播放新回复前先停掉仍在播的旧音频（防两个回复语音重叠）
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
      const audio = new Audio(audioUrl);
      audio.playbackRate = 1.25;   // 语速 1.25 倍（2026-08-16 深夜：1.5x 太快，回调到 1.25x）
      audioRef.current = audio;
      audio.onended = () => {
        suppressRef.current = false;
        busyRef.current = false;
        audioRef.current = null;
        URL.revokeObjectURL(audioUrl);
        suppressStartRef.current = 0;
        setStatus('正在监听...');
      };
      try {
        await audio.play();
      } catch (e) {
        // 播放失败（如自动播放策略限制）：恢复监听状态
        suppressRef.current = false;
        busyRef.current = false;
        audioRef.current = null;
        suppressStartRef.current = 0;
        setStatus('正在监听...');
      }
    } catch (e) {
      // TTS 失败不卡死：对话已落库，提示具体原因后恢复监听
      console.error('tts failed:', e);
      busyRef.current = false;
      suppressRef.current = false;
      suppressStartRef.current = 0;
      setStatus('语音合成失败，已显示文字回复');
    }
  };

  // 重播上一条回复：按钮点击时调用（gen 用当前代际，若此刻正在打断/卸载则自然跳过）
  const replayLast = async () => {
    const text = lastReplyRef.current;
    if (!text) return;
    speechQueueRef.current = [];   // 终止句级 TTS 链（避免与整段重播串行打架）
    speechPlayingRef.current = false;
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setStatus('重播回复中...');
    await playReply(text, genRef.current);
  };

  // 句级 TTS 播放链：从队列取一句合成播放，播完取下一句；队列空且本轮流结束即收尾。
  // 边流式边出声（2026-08-16 晚提速）：首句在 onDelta 就绪即播放，不再等整段回复流完，
  // 回复语音不再滞后已流式显示的文字 2~3s。打断/重播/卸载经由 gen 与 speechPlayingRef 保护。
  const kickSpeech = (gen) => {
    if (gen !== genRef.current || !aliveRef.current) return;
    // 未聚焦或静音：不播声音（回复文字已渲染/落库），丢弃待播句子
    if (!pageFocusedRef.current || voiceMutedRef.current) {
      speechQueueRef.current = [];
      speechPlayingRef.current = false;
    }
    if (speechQueueRef.current.length === 0) {
      // 队列空 + 本轮流已结束 → 语音收尾，恢复 VAD 正常处理
      if (replyFinishedRef.current && !speechPlayingRef.current) {
        suppressRef.current = false;
        suppressStartRef.current = 0;
        busyRef.current = false;
        setStatus('正在监听...');
      }
      return;
    }
    if (speechPlayingRef.current) return;
    const sentence = speechQueueRef.current.shift();
    speechPlayingRef.current = true;
    setStatus('播放回复中...');   // 播放期间仍监听，用户说话即打断
    suppressRef.current = true;
    suppressStartRef.current = performance.now();   // 预热期起点：只校准 baseAvg 不判定打断
    (async () => {
      try {
        // 播下句前先停旧音频（防两个回复语音重叠）
        if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
        const audioUrl = await voiceApi.tts(stripMarkdown(sentence));
        if (gen !== genRef.current || !aliveRef.current) {
          URL.revokeObjectURL(audioUrl);
          speechPlayingRef.current = false;
          return;
        }
        const audio = new Audio(audioUrl);
        audio.playbackRate = 1.25;   // 语速 1.25 倍（与 playReply 一致，2026-08-16 深夜回调）
        audioRef.current = audio;
        audio.onended = () => {
          audioRef.current = null;
          URL.revokeObjectURL(audioUrl);
          speechPlayingRef.current = false;
          kickSpeech(gen);
        };
        try {
          await audio.play();
        } catch (e) {
          // 播放失败（如自动播放策略限制）：恢复监听状态
          audioRef.current = null;
          URL.revokeObjectURL(audioUrl);
          speechPlayingRef.current = false;
          kickSpeech(gen);
        }
      } catch (e) {
        // 单句 TTS 失败不卡死：末句失败且无后续句子时提示，否则继续播下一句
        console.error('tts sentence failed:', e);
        speechPlayingRef.current = false;
        kickSpeech(gen);
        if (speechQueueRef.current.length === 0 && !speechPlayingRef.current && replyFinishedRef.current) {
          setStatus('语音合成失败，已显示文字回复');
        }
      }
    })();
  };

  // textarea 自动增高（2026-09-01 起：多行输入保真换行，粘贴长文本不被单行 input 吞掉）
  const autoGrowInput = () => {
    const el = textAreaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 6 * 24 + 16)}px`;
  };

  // 统一发送一轮对话：停止旧音频 → 建 user/assistant turn → busy → streamChat → finish/句级TTS
  const sendTurn = async (text) => {
    // 语音流在途时又来一轮（文字输入/转写并发）：先打断旧流，避免两个 SSE 流并存内容串扰。
    // 语音路径（flushPendingTurn）触发时 busy 已复位，不受影响（2026-08-17 修）
    if (busyRef.current) interrupt();
    const gen = genRef.current;   // 记录本轮的对话代际，被打断（gen 变化）后不再播放旧回复
    // 本轮句级 TTS 播放链初始化（打断/重播可能残留上一轮状态）
    speechQueueRef.current = [];
    speechCursorRef.current = 0;
    speechPlayingRef.current = false;
    replyFinishedRef.current = false;
    // 本轮累积文本复位（打断时固定到面板用）
    assistantTextRef.current = '';
    thinkingTextRef.current = '';
    try {
      if (!chatConfiguredRef.current) {
        setStatus('未配置对话模型，请到设置页配置');
        return;
      }
      // 用户说了新话：立即停止正在播放的旧回复，不再继续念旧句（防语音重叠）
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
      setStatus(`你说：${text.length > 50 ? `${text.slice(0, 50)}…` : text}`);
      // 用户消息：语音路径已由 processVoice 逐段实时上屏（activeUserTurnIdRef 已存在）；
      // 文字输入路径在此创建。创建后立即封口，后续新语音另起新 turn。
      if (!activeUserTurnIdRef.current) {
        const uid = `turn_${++turnSeq}`;
        setTurns((prev) => [...prev, { id: uid, role: 'user', content: text }]);
      }
      activeUserTurnIdRef.current = null;
      // 转录面板：记录本轮 assistant 占位（思考过程 + Markdown 回复）
      const assistantTurn = { id: `turn_${++turnSeq}`, role: 'assistant', content: '', thinking: '' };
      setTurns((prev) => [...prev, assistantTurn]);
      setStreamingId(assistantTurn.id);
      busyRef.current = true;   // 思考期间也持续监听，用户开口即打断

      let assistantText = '';
      let thinkingText = '';
      let failed = null;   // {msg, code}：SSE 错误帧或网络层失败，finish 时展示具体原因

      const patchTurn = (patch) => {
        setTurns((prev) => {
          const last = prev[prev.length - 1];
          if (!last || last.role !== 'assistant') return prev;
          return [...prev.slice(0, -1), { ...last, ...patch }];
        });
      };
      // 桌面助手不采集 thinking（onThinking 不传）：思考打字机/思考块仅在聊天页文字输入使用。
      // 保留 flushThinking 兜底（thinkingPendingRef 恒空、无副作用），与 interrupt/卸载清理逻辑兼容。
      const flushThinking = () => {
        if (thinkingPendingRef.current) {
          thinkingText += thinkingPendingRef.current;
          thinkingPendingRef.current = '';
        }
        if (thinkingTimerRef.current) {
          clearInterval(thinkingTimerRef.current);
          thinkingTimerRef.current = null;
        }
        thinkingTextRef.current = thinkingText;
      };
      const finish = async () => {
        flushThinking();   // 流结束把剩余未显示的思考一次性补全
        patchTurn({ thinking: thinkingText, content: assistantText });   // 补全后同步一次最终状态
        setStreamingId(null);
        abortRef.current = null;
        if (failed) {
          // 出错：清空句级 TTS 队列（已入队但未播的句子不再播），停止当前播放
          speechQueueRef.current = [];
          speechPlayingRef.current = false;
          if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }
          patchTurn({ error: friendlyError(failed.msg, failed.code) });
          busyRef.current = false;
          suppressRef.current = false;
          suppressStartRef.current = 0;
          setStatus(friendlyError(failed.msg, failed.code));   // 展示具体原因，不再笼统"出错请重试"
          setIsListening(true);
          return;
        }
        // 把剩余未入队的尾句入队，标记本轮结束，交由句级 TTS 链播放并收尾。
        // 此前 finish 直接对整段合成播放（playReply），长回复要等全部文字流完才开始出声。
        const tail = assistantText.slice(speechCursorRef.current).trim();
        if (tail) speechQueueRef.current.push(tail);
        speechCursorRef.current = assistantText.length;
        replyFinishedRef.current = true;
        lastReplyRef.current = assistantText;
        setLastReply(assistantText);
        kickSpeech(gen);
      };

      const controller = streamChat({
        message: text,
        sessionId: sessionIdRef.current,
        source: 'desktop',   // 桌面助手语音：后端按 source 分流 thinking=low 浅度思考快速回话（见 AGENTS.md）
        onSession: (sid) => {
          // 后端可能自愈新建会话（如固定会话被软删），同步最新 id 避免碎片化
          sessionIdRef.current = sid;
          localStorage.setItem(DESKTOP_SESSION_KEY, sid);
        },
        onDelta: (d) => {
          if (!aliveRef.current) return;
          assistantText += d;
          assistantTextRef.current = assistantText;
          patchTurn({ content: assistantText, thinking: thinkingText });
          // 句级 TTS：完整句子尽早入队并播放，不等整段回复流完（回复出声不再滞后文字 2~3s）
          const head = assistantText.slice(speechCursorRef.current);
          const boundary = lastSentenceBoundary(head);
          if (boundary > 0) {
            const sentence = head.slice(0, boundary).trim();
            speechCursorRef.current += boundary;
            if (sentence) speechQueueRef.current.push(sentence);
            kickSpeech(gen);
          }
        },
        // 桌面助手不采集 thinking（onThinking 故意不传）：深度思考块只保留在聊天页文字输入
        //（2026-08-19 起，见 AGENTS.md 思考流约定）；thinkingPendingRef 恒空、思考打字机不启动。
        onError: (msg, code) => {
          failed = { msg, code };
          finish();
        },
        onDone: finish,
      });
      abortRef.current = controller;
    } catch (err) {
      // 建流失败不卡死：提示错误，VAD 继续监听
      console.error('sendTurn failed:', err);
      suppressRef.current = false;
      busyRef.current = false;
      setStreamingId(null);
      // 后端返回了具体原因（如音频转写失败）则展示，否则按网络异常提示
      const detail = err?.response?.data?.detail;
      const isNetwork = !detail || err?.code === 'ERR_NETWORK' || err?.code === 'ECONNABORTED';
      setStatus(isNetwork ? friendlyError('', 'network_error') : detail);
      setIsListening(true);
    }
  };

  // 录音段转写结果：追加进合并缓冲，停顿 MERGE_WINDOW_MS 后统一发送（避免一句话拆多个回复）
  const processVoice = async (blob) => {
    const gen = genRef.current;   // 记录本段语音的代际，被打断（gen 变化）后立即丢弃
    try {
      const text = await voiceApi.transcribe(blob);
      if (gen !== genRef.current || !aliveRef.current) return;
      if (!text) {
        // 空转写（未捕捉到有效语音/环境声误触发）：静默恢复监听，不显示"出错"
        setStatus('正在监听...');
        return;
      }
      pendingTextRef.current = (pendingTextRef.current + text).trim();
      // 实时上屏：段转写完成即追加到转录面板当前用户消息（边说边上屏，不等合并窗口结束）
      if (activeUserTurnIdRef.current) {
        const uid = activeUserTurnIdRef.current;
        setTurns((prev) => prev.map((t) => (t.id === uid ? { ...t, content: t.content + text } : t)));
      } else {
        const uid = `turn_${++turnSeq}`;
        activeUserTurnIdRef.current = uid;
        setTurns((prev) => [...prev, { id: uid, role: 'user', content: text }]);
      }
      // 状态栏提示已听到的内容（超 50 字截断）
      const shown = pendingTextRef.current.length > 50
        ? `${pendingTextRef.current.slice(0, 50)}…`
        : pendingTextRef.current;
      setStatus(`已听到：${shown}（停顿 2.5 秒后自动发送）`);
      // 合并窗口：一律挂 2.5s 窗口等继续说（checkVolume 在录音中/检测到声音时持续 clear 窗口，
      // 静默到点 flush 自动发送）——此前"短确认窗立即 flush"在中文换气/想词停顿处仍会把
      // 一句话拆成多轮回复（2026-08-16 晚审查发现）。
      if (finalizeTimerRef.current) {
        clearTimeout(finalizeTimerRef.current);
        finalizeTimerRef.current = null;
      }
      finalizeTimerRef.current = setTimeout(flushPendingTurn, MERGE_WINDOW_MS);
      // 防无限累积：缓冲过长立即 flush
      if (pendingTextRef.current.length > MAX_PENDING_LEN) flushPendingTurn();
    } catch (err) {
      // 转写失败不卡死：提示错误，VAD 继续监听
      console.error('transcribe failed:', err);
      // 展示后端具体原因（如「音频转写失败…」），仅真实网络错误才显示「网络异常」
      //（AGENTS.md：错误提示展示具体原因，勿笼统「出错请重试」）
      const detail = err?.response?.data?.detail;
      const isNetwork = !detail || err?.code === 'ERR_NETWORK' || err?.code === 'ECONNABORTED';
      setStatus(isNetwork ? friendlyError('', 'network_error') : detail);
    }
  };

  const stopListening = () => {
    startGenRef.current++;   // 令在途 startListening 代际失效（await getUserMedia 恢复后即释放流）
    // 1) 取消 VAD 循环，避免组件卸载后 rAF 空转（浏览器麦克风指示灯残留的根因）
    if (rafIdRef.current != null) {
      cancelAnimationFrame(rafIdRef.current);
      rafIdRef.current = null;
    }
    // 2) 停止正在播放的回复音频（离开页面时 TTS 立即中断）
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    suppressRef.current = false;
    busyRef.current = false;
    // 清打断开录/丢弃标记：避免"主动停录的尾段"被误判为未确认瞬态段而丢弃不落库
    armedRef.current = false;
    dropNextRef.current = false;
    interruptAccumRef.current = 0;
    belowSinceRef.current = 0;
    suppressStartRef.current = 0;
    // 页面离开：取消合并窗口（pendingTextRef 保留，回到页面后重新挂窗口不丢已听内容）
    if (finalizeTimerRef.current) {
      clearTimeout(finalizeTimerRef.current);
      finalizeTimerRef.current = null;
    }
    // 终止句级 TTS 播放链（audioRef 已 pause，onended 不会触发，须手动复位）
    speechQueueRef.current = [];
    speechPlayingRef.current = false;
    // 停止思考打字机定时器并清空队列（页面离开后不再揭晓旧思考）
    if (thinkingTimerRef.current) {
      clearInterval(thinkingTimerRef.current);
      thinkingTimerRef.current = null;
    }
    thinkingPendingRef.current = '';
    // 3) 若正在录音，先 stop 让最后一段走 onstop → processVoice 落库（不中断在途对话）
    if (mediaRecorderRef.current && mediaRecorderRef.current.state === 'recording') {
      try { mediaRecorderRef.current.stop(); } catch (e) { /* already stopped */ }
    }
    recordingRef.current = false;
    // 4) 第一时间停掉 getUserMedia 的所有 tracks，立即关闭麦克风
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    // 兜底：recorder.stream 的 tracks（streamRef 缺失/异常时）
    if (mediaRecorderRef.current) {
      try { mediaRecorderRef.current.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* noop */ }
    }
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
    // 移除 AudioContext 用户激活兜底监听器（若尚未触发）
    if (gestureResumeRef.current) {
      document.removeEventListener('pointerdown', gestureResumeRef.current);
      document.removeEventListener('keydown', gestureResumeRef.current);
      gestureResumeRef.current = null;
    }
    clearTimeout(silenceTimerRef.current);
    // 停止后清空 recorder 引用，避免复用已绑定旧 stream 的 recorder
    mediaRecorderRef.current = null;
    // 注意：此处**不能**提前清 chunksRef —— recorder.stop() 的 onstop 是异步触发的，
    // 最后一段录音要在 onstop 里经 blobToWav16k → processVoice 落库（否则末段语音丢失，
    // 2026-08-24 修）；chunksRef 由 onstop 在构建完 blob 后自行清空
    setIsListening(false);
    setVolume(0);
    setStatus('已停止监听');
  };

  return (
    <div className="p-6 flex flex-col items-center h-full relative">
      {!chatConfigured && (
        <div className="w-full max-w-md mb-4 px-4 py-2 bg-amber-50 border border-amber-200 text-amber-700 text-sm rounded-lg">
          您还没有配置对话模型，请到
          <a href="/settings" className="mx-1 underline font-medium">设置页</a>
          配置后再开始对话。
        </div>
      )}
      <h1 className="text-2xl font-bold">桌面助手</h1>
      <p className="text-slate-500">打开本页面即开始实时语音对话，关闭页面则停止监听。</p>
      <div className="mt-4 flex items-center gap-3">
        <button
          onClick={() => setVoiceMuted((v) => !v)}
          className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
            voiceMuted
              ? 'bg-slate-100 border-slate-300 text-slate-600'
              : 'bg-indigo-600 border-indigo-600 text-white hover:bg-indigo-700'
          }`}
        >
          {voiceMuted ? '🔇 语音回复已静音' : '🔊 语音回复开启'}
        </button>
        {lastReply && (
          <button
            onClick={replayLast}
            className="px-3 py-1.5 rounded-lg text-sm font-medium bg-white border border-slate-300 text-slate-600 hover:bg-slate-50"
          >
            重播上一条
          </button>
        )}
      </div>
      <div className={`mt-8 w-32 h-32 rounded-full flex items-center justify-center text-white shadow-lift ${
        isListening ? 'bg-gradient-to-r from-indigo-500 to-violet-500' : 'bg-slate-300'
      }`}>
        {isListening ? '监听中' : '已停止'}
      </div>
      {/* 实时音量反馈：VAD level 节流渲染（说话时可见条幅，静默时接近 0） */}
      <div className="mt-3 w-40 h-1.5 bg-slate-200 rounded-full overflow-hidden">
        <div
          className="h-full bg-indigo-500 rounded-full transition-[width] duration-150"
          style={{ width: `${isListening ? volume : 0}%` }}
        />
      </div>
      <p className="mt-3 text-slate-600">{status}</p>
      {micError && (
        <div className="mt-3 flex flex-col items-center gap-2">
          <p className="text-sm text-red-500 max-w-md text-center">{micError}</p>
          <button
            onClick={startListening}
            className="px-4 py-1.5 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700"
          >
            重新启动监听
          </button>
        </div>
      )}

      {/* 转录面板：每轮「用户话 → 深度思考 → Markdown 回复」 */}
      <div ref={transcriptRef} className="mt-6 w-full max-w-2xl flex-1 overflow-auto min-h-0 bg-white border border-slate-200 rounded-xl p-4 shadow-card">
        {turns.length === 0 ? (
          <p className="text-center text-slate-400 text-sm py-10">你说的话和回复会显示在这里</p>
        ) : (
          turns.map((t) => (
            <ChatMessage key={t.id} message={t} streaming={t.id === streamingId} showThinking={false} />
          ))
        )}
      </div>
      {/* 文字输入兜底：麦克风/识别故障、或不想说话时仍可对话（与语音并行，均可打断回复） */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const t = textInput.trim();
          if (!t) return;
          setTextInput('');
          sendTurn(t);
        }}
        className="mt-4 w-full max-w-2xl flex gap-2"
      >
        <textarea
          ref={textAreaRef}
          rows={1}
          value={textInput}
          onChange={(e) => setTextInput(e.target.value)}
          onInput={autoGrowInput}
          onKeyDown={(e) => {
            // Enter 发送、Shift+Enter 换行（2026-09-01 起：多行输入，IME 组合态不发送）
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              const t = textInput.trim();
              if (!t) return;
              setTextInput('');
              sendTurn(t);
            }
          }}
          placeholder="也可以打字提问，Enter 发送，Shift+Enter 换行（语音识别不可用时仍可对话）"
          className="input-base flex-1 resize-none overflow-hidden leading-6"
        />
        <button type="submit" className="btn-primary px-4 py-2 rounded-lg">发送</button>
      </form>
    </div>
  );
}
