import { api } from './client';

// 多 Agent 任务 API（02 §5/§6）：任务列表 / 详情 / 产物下载 / 取消
export const agentApi = {
  listTasks: () => api.get('/api/agent/tasks').then((r) => r.data),
  getTask: (id) => api.get(`/api/agent/tasks/${id}`).then((r) => r.data),
  cancelTask: (id) => api.post(`/api/agent/tasks/${id}/cancel`).then((r) => r.data),
  // 产物下载：直接走浏览器（后端已做认证 + 路径隔离）
  artifactUrl: (taskId, name) => `/api/agent/tasks/${taskId}/artifacts/${encodeURIComponent(name)}`,
};
