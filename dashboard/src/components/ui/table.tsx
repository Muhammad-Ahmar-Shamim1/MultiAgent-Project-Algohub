import * as React from "react";

import { cn } from "@/lib/utils";

/** A table that scrolls sideways inside its own box instead of widening the page (360 px). */
export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto rounded-lg border">
      <table className={cn("w-full min-w-max text-left text-sm", className)} {...props} />
    </div>
  );
}

export function Th({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn("border-b bg-muted px-3 py-2 text-xs font-semibold uppercase", className)}
      {...props}
    />
  );
}

export function Td({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b px-3 py-2 align-top", className)} {...props} />;
}
