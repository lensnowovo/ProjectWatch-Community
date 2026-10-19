import type { ReactNode } from "react";
import BlurText from "./bits/BlurText";

export type HeaderVariant = "feed" | "portfolio" | "digest" | "plain";
const directions = { feed: "top", portfolio: "bottom", digest: "left", plain: "bottom" } as const;

/** Compact page identity: title, one line of context and actions; the first screen belongs to the information below. */
export default function PageHeader({ title, label, description, meta, action, variant = "plain" }: {
  title: string;
  label: string;
  description: string;
  meta?: ReactNode;
  action?: ReactNode;
  variant?: HeaderVariant;
}) {
  return (
    <header className="page-header" data-variant={variant}>
      <div className="page-header-row">
        <div className="page-header-copy">
          <span className="page-header-label">{label}</span>
          <h1><BlurText key={title} text={title} direction={directions[variant]} /></h1>
          <p>{description}</p>
        </div>
        {action && <div className="page-header-action">{action}</div>}
      </div>
      {meta && <div className="page-header-meta">{meta}</div>}
    </header>
  );
}
