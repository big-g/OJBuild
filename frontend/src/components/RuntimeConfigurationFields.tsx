import type { RuntimeAdapter, RuntimeConfig } from '../lib/runtime-tools-api';

export function RuntimeConfigurationFields({ adapter, config, onChange }: {
  adapter: RuntimeAdapter;
  config: RuntimeConfig;
  onChange: (config: RuntimeConfig) => void;
}) {
  const className = 'w-full rounded-lg px-3 py-2 text-sm border';
  const style = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', borderColor: 'var(--color-border)' };
  return <>{adapter.fields.map(field => {
    const value = config[field.name] ?? adapter.default_config[field.name] ?? '';
    return <label key={field.name} className="block text-sm">{field.label}
      {field.type === 'select'
        ? <select className={className} style={style} required={field.required} value={String(value)}
          onChange={e => onChange({ ...config, [field.name]: e.target.value })}>
          {field.options?.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        : <input className={className} style={style} required={field.required} maxLength={field.max_length}
          placeholder={field.placeholder} value={Array.isArray(value) ? value.join(', ') : value}
          onChange={e => onChange({ ...config, [field.name]: field.type === 'string_list'
            ? e.target.value.split(',').map(v => v.trim()) : e.target.value })} />}
      {field.description && <span className="block text-xs mt-1" style={{ color: 'var(--color-text-secondary)' }}>{field.description}</span>}
    </label>;
  })}</>;
}

export function RuntimeConfigurationSummary({ adapter, config }: { adapter?: RuntimeAdapter; config: RuntimeConfig }) {
  return <dl className="text-sm space-y-1">{Object.entries(config).map(([key, value]) => {
    const field = adapter?.fields.find(f => f.name === key);
    const display = Array.isArray(value) ? value.join(', ') : field?.options?.find(o => o.value === value)?.label || value;
    return <div key={key} className="flex flex-wrap gap-x-2"><dt>{field?.label || key}:</dt><dd className="break-all">{display}</dd></div>;
  })}</dl>;
}
