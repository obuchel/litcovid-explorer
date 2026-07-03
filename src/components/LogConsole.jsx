import { useEffect, useRef } from 'react';

export default function LogConsole({ lines }) {
  const ref = useRef(null);

  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines]);

  if (!lines.length) return null;

  return (
    <div className="log-console" ref={ref}>
      {lines.map((l, i) => (
        <div key={i} className={`log-line ${l.level}`}>
          {l.message}
        </div>
      ))}
    </div>
  );
}
