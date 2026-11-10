import apiClient from './index';

export interface FreeModelCatalog {
  checked_at: number;
  models: { id: string; name: string; input_price: number; output_price: number }[];
}
export interface FreeModelStatus {
  enabled: boolean;
  model: string;
  daily_limit: number;
  used_today: number;
  catalog: FreeModelCatalog | null;
  recent_calls: { model: string; status: string; tokens: number | null; elapsed_ms: number; code: string | null; finished: number }[];
}
export const freeModelsApi = {
  status: async () => (await apiClient.get<FreeModelStatus>('/api/v1/free-models/status')).data,
  refresh: async () => (await apiClient.post<FreeModelCatalog>('/api/v1/free-models/refresh', {}, { timeout: 40000 })).data,
  test: async (model: string) => (await apiClient.post<{
    success: boolean; model: string; reply?: string; message?: string; code?: string;
    usage?: { total_tokens?: number } | null; status: FreeModelStatus;
  }>('/api/v1/free-models/test', { model }, { timeout: 90000 })).data,
};
