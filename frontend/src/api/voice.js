import { api } from './client';

export const voiceApi = {
  // 上传音频转文字（ASR），需认证
  // 2026-08-16 起浏览器侧统一先解码为 16kHz 单声道 WAV 再上传（见 utils/audio.js），
  // 后端对 16kHz mono WAV 直接透传不再走 ffmpeg
  transcribe: (blob) => {
    const form = new FormData();
    form.append('file', blob, 'voice.wav');
    return api.post('/api/voice/transcribe', form).then((r) => r.data.text);
  },
  // 文字合成语音（TTS）：接口返回音频文件流，这里以 blob 接收并生成可播放 URL
  tts: async (text) => {
    const res = await api.post('/api/voice/tts', { text }, { responseType: 'blob' });
    return URL.createObjectURL(res.data);   // 调用方播放后须 revokeObjectURL
  },
};
