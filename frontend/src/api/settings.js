import { api } from './client';

export const settingsApi = {
  getModels: () => api.get('/api/settings/models').then((r) => r.data),
  addModel: (model) => api.post('/api/settings/models', model).then((r) => r.data),
  testModel: (id) => api.post(`/api/settings/models/${id}/test`).then((r) => r.data),
  updateModel: (id, model) => api.put(`/api/settings/models/${id}`, model).then((r) => r.data),
  deleteModel: (id) => api.delete(`/api/settings/models/${id}`).then((r) => r.data),
  getFunctionModels: () => api.get('/api/settings/function-models').then((r) => r.data),
  assignFunctionModel: (func, modelId) =>
    api.put('/api/settings/function-models', { func, model_id: modelId }).then((r) => r.data),
  // 清空数据：密码放 JSON body（接口见 09_数据管理详解.md）
  clearData: (type, password) =>
    api.post('/api/data/clear', { type, password }).then((r) => r.data),
  // 为当前账号补录手机号 + 密码（10 §5.1：飞书绑定改在飞书对话框内三步完成）
  bindPhone: ({ phone, code, password }) =>
    api.post('/api/settings/bind-phone', { phone, code, password }).then((r) => r.data),
  // 飞书账号绑定状态 / 解绑 / 修改密码（10 §5.1，2026-08-18 起）
  getFeishuBindStatus: () => api.get('/api/settings/feishu-bind-status').then((r) => r.data),
  unbindFeishu: ({ openId, phone, code }) =>
    api.post('/api/settings/feishu-unbind', { open_id: openId, phone, code }).then((r) => r.data),
  changePassword: ({ phone, code, newPassword }) =>
    api.post('/api/settings/change-password', { phone, code, new_password: newPassword }).then((r) => r.data),
  // 电商数据源 API key 配置（02 爬取 §7.1，2026-08-19 起）
  getDataSources: () => api.get('/api/settings/datasources').then((r) => r.data),
  saveDataSource: ({ source, apiKey }) =>
    api.put('/api/settings/datasources', { source, api_key: apiKey }).then((r) => r.data),
};
