/** Original eight-tooth line icon, shared between navigation and settings cover. */
const teeth = Array.from({ length: 32 }, (_, i) => {
  const angle = i * Math.PI / 16 - Math.PI / 2;
  const radius = i % 4 === 1 || i % 4 === 2 ? 20 : 16;
  return `${(24 + Math.cos(angle) * radius).toFixed(2)},${(24 + Math.sin(angle) * radius).toFixed(2)}`;
}).join(" ");

export default function SettingsGear({ className = "", size = 20 }: { className?: string; size?: number }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 48 48"
      fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <polygon points={teeth} />
      <circle cx="24" cy="24" r="7" />
    </svg>
  );
}
