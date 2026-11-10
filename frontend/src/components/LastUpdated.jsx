import { useEffect, useState } from 'react';

// "just now" / "7s ago" / a clock time. It ticks on its own so the rest of the page does not re-render.
export default function LastUpdated({ date }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  if (!date) return '—';
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 2) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  return date.toLocaleTimeString();
}
