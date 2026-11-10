import type { ReactNode } from "react";
import BlurText from "./bits/BlurText";

export type HeaderVariant = "feed" | "portfolio" | "digest" | "plain";
const directions = { feed: "top", portfolio: "bottom", digest: "left", plain: "bottom" } as const;

export default function PageHeader({ title, meta, action, variant = "plain" }: {
  title: string;
  meta?: ReactNode;
  action?: ReactNode;
  variant?: HeaderVariant;
}) {
  return (
    <header className="page-header" data-variant={variant}>
      <div className="page-header-row">
        <div className="page-header-copy">
          <h1><BlurText key={title} text={title} direction={directions[variant]} /></h1>
        </div>
        {action && <div className="page-header-action">{action}</div>}
      </div>
      {meta && <div className="page-header-meta">{meta}</div>}
    </header>
  );
}
