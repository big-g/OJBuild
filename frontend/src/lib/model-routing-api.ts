import { apiFetch } from './api';

export type RoutingTask = 'general' | 'coding' | 'analysis' | 'vision';
export const ROUTING_TASKS: RoutingTask[] = ['general', 'coding', 'analysis', 'vision'];
export interface TaskRule {
  task: RoutingTask; revision: number; enabled: boolean; model_id: string; benchmark_id: string;
  fallback_model_id?: string; fallback_benchmark_id?: string;
}
export interface DiagnosticResult {
  id: string; seq: number; model_id: string; task: RoutingTask;
  connection_revision: number; suite_version: string; passed: boolean;
  elapsed_ms: number; tokens: number; timestamp: number;
  details: { task_passed: boolean; tools_passed: boolean; tools_tested: boolean; failure: string; score?: number; cases?: { name: string; passed: boolean; elapsed_ms: number }[] };
}
export interface RoutingConfiguration {
  rules: TaskRule[]; benchmarks: DiagnosticResult[]; suite_version: string;
  audit: { task: string; revision: number; event: string; actor: string; timestamp: number }[];
}
async function request<T>(path = '', method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`/v1/model-routing${path}`, {
    method, headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof value?.detail === 'string' ? value.detail : `Model routing failed (${response.status})`);
  return value as T;
}
export const getModelRouting = () => request<RoutingConfiguration>();
export const runModelDiagnostic = (model_id: string, connection_revision: number, task: RoutingTask) =>
  request<DiagnosticResult>('/benchmarks', 'POST', { model_id, connection_revision, task });
export const saveTaskRule = (rule: TaskRule) => request<TaskRule>(`/tasks/${rule.task}`, 'PUT', {
  revision: rule.revision, enabled: rule.enabled, model_id: rule.model_id, benchmark_id: rule.benchmark_id,
  ...(rule.fallback_model_id !== undefined ? { fallback_model_id: rule.fallback_model_id, fallback_benchmark_id: rule.fallback_benchmark_id || '' } : {}),
});
