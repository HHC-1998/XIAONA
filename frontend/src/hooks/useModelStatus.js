import { useEffect, useState, useCallback } from 'react';
import { settingsApi } from '../api/settings';

// 读取设置页功能模型分配状态（聊天/记忆抽取/向量化），供聊天、关系网、桌面助手页提示缺失。
// 后端 GET /api/settings/function-models 返回 chat_configured / extract_configured / embedding_configured。
export function useModelStatus() {
  const [status, setStatus] = useState({
    chat_model_id: '',
    extract_model_id: '',
    embedding_model_id: '',
    chat_configured: false,
    extract_configured: false,
    embedding_configured: false,
  });
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(() => {
    return settingsApi
      .getFunctionModels()
      .then(setStatus)
      .catch(() => {})   // 接口失败不阻塞页面
      .finally(() => setLoaded(true));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { ...status, loaded, refresh };
}
