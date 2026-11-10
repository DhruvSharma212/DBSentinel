import React, { useState } from 'react';
import { Cpu, HardDrive, MemoryStick, Search } from 'lucide-react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { fmtBytes, fmtClock, fmtRate } from '../lib/format.js';
import { levelFor, processMemoryLevel } from '../lib/thresholds.js';
import { BarCell, ChartCard, Drawer, Empty, Metric, PageIntro, Table } from '../components/ui.jsx';

const TOOLTIP_STYLE = { background: '#101827', border: '1px solid #263449', borderRadius: 10 };

export default function OSMonitor({ dash }) {
  const { data } = dash;
  const os = data?.os;
  const processes = data?.processes || [];
  const history = data?.history || [];
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);

  const needle = search.toLowerCase();
  const shown = processes.filter((p) => `${p.name} ${p.pid} ${p.username || ''}`.toLowerCase().includes(needle));
  const hasRates = Number.isFinite(os?.disk_read_bps) && Number.isFinite(os?.disk_write_bps);
  const diskSub = `${fmtBytes(os?.disk_free)} free` + (hasRates ? ` · read ${fmtRate(os.disk_read_bps)} · write ${fmtRate(os.disk_write_bps)}` : '');
  const throughput = history.filter((h) => Number.isFinite(h.read_bps) || Number.isFinite(h.write_bps));

  return (
    <>
      <PageIntro eyebrow="HOST TELEMETRY" title="OS Monitor" desc="Inspect live host utilization, disk volumes, and resource-heavy processes." />
      <div className="metrics three">
        <Metric icon={Cpu} label="CPU" value={os?.cpu_percent?.toFixed(1) ?? '—'} unit="%" percent={os?.cpu_percent}
          level={levelFor('cpu', os?.cpu_percent)} spark={history.slice(-30).map((h) => h.cpu)} sub={`${os?.cpu_count || '—'} logical cores`} />
        <Metric icon={MemoryStick} label="MEMORY" value={os?.memory_percent?.toFixed(1) ?? '—'} unit="%" percent={os?.memory_percent} tone="violet"
          level={levelFor('memory', os?.memory_percent)} spark={history.slice(-30).map((h) => h.memory)} sub={`${fmtBytes(os?.memory_available)} available`} />
        <Metric icon={HardDrive} label="DISK" value={os?.disk_percent?.toFixed(1) ?? '—'} unit="%" percent={os?.disk_percent} tone="blue"
          level={levelFor('disk', os?.disk_percent)} sub={diskSub} />
      </div>

      <div className="charts-grid single">
        <ChartCard title="Disk throughput" subtitle="Bytes read and written per second, measured between snapshots">
          {throughput.length > 1 ? (
            <>
              <div className="chart">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={throughput}>
                    <defs>
                      <linearGradient id="readFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#7c9cff" stopOpacity={0.28} /><stop offset="95%" stopColor="#7c9cff" stopOpacity={0} /></linearGradient>
                      <linearGradient id="writeFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#f5ad62" stopOpacity={0.28} /><stop offset="95%" stopColor="#f5ad62" stopOpacity={0} /></linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                    <XAxis dataKey="timestamp" tickFormatter={fmtClock} minTickGap={28} stroke="#64748b" fontSize={10} />
                    <YAxis tickFormatter={(v) => fmtBytes(v)} stroke="#64748b" fontSize={10} width={62} />
                    <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={(v) => new Date(v).toLocaleTimeString()} formatter={(v) => fmtRate(Number(v))} />
                    <Area type="monotone" dataKey="read_bps" name="Read" stroke="#7c9cff" fill="url(#readFill)" strokeWidth={2} isAnimationActive={false} />
                    <Area type="monotone" dataKey="write_bps" name="Write" stroke="#f5ad62" fill="url(#writeFill)" strokeWidth={2} isAnimationActive={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
              <div className="legend">
                <span><i className="blue-dot" /> Read</span>
                <span><i className="orange-dot" /> Write</span>
              </div>
            </>
          ) : <Empty title="Collecting throughput" desc="Rates appear after the second snapshot." />}
        </ChartCard>
      </div>

      <section className="card tablecard">
        <div className="section-head"><div><h3>Mounted volume usage</h3><p>Read-only disk usage by accessible volume</p></div></div>
        <Table
          headers={['VOLUME', 'MOUNT', 'FILESYSTEM', 'USED', 'FREE', 'USAGE']}
          rows={(os?.partitions || []).map((d) => [
            d.device, d.mountpoint, d.filesystem, fmtBytes(d.used), fmtBytes(d.free),
            <><b>{d.percent.toFixed(1)}%</b><div className="progress"><i style={{ width: Math.min(100, d.percent) + '%' }} /></div></>,
          ])}
          empty="No accessible mounted volumes"
        />
      </section>

      <section className="card tablecard">
        <div className="section-head">
          <div>
            <h3>Process explorer</h3>
            <p>{os?.process_count ?? 0} processes discovered · the {processes.length} busiest by CPU or memory are shown, sorted by CPU usage (% of total CPU capacity). Disk I/O counters are cumulative where OS permissions allow.</p>
          </div>
          <div className="searchbox">
            <Search size={15} />
            <input placeholder="Search name, PID, user…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
        </div>
        <Table
          headers={['PROCESS', 'PID', 'USER', 'CPU %', 'MEMORY %', 'READ', 'WRITTEN', 'DETAILS']}
          rows={shown.map((p) => [
            <b>{p.name}</b>, p.pid, p.username || '—',
            <BarCell value={p.cpu_percent} level={levelFor('cpu', p.cpu_percent)} />,
            <BarCell value={p.memory_percent} level={processMemoryLevel(p.memory_percent)} />,
            p.disk_read_bytes == null ? '—' : fmtBytes(p.disk_read_bytes),
            p.disk_write_bytes == null ? '—' : fmtBytes(p.disk_write_bytes),
            <button className="smallbtn" onClick={() => setSelected(p)}>Inspect</button>,
          ])}
        />
      </section>

      {selected && (
        <Drawer title={`Process · ${selected.name}`} close={() => setSelected(null)}>
          <div className="detailgrid">
            {Object.entries(selected).map(([k, v]) => <div key={k}><span>{k.replaceAll('_', ' ')}</span><b>{String(v ?? '—')}</b></div>)}
          </div>
          <p className="mutedline">Read-only process details. DBSentinel does not terminate processes.</p>
        </Drawer>
      )}
    </>
  );
}
