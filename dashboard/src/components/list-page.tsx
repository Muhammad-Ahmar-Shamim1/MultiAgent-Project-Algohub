"use client";

import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { PageHeader, QueryState } from "@/components/common";
import { Input, Label, Select } from "@/components/ui/form";
import { Table, Td, Th } from "@/components/ui/table";
import { humanize } from "@/lib/format";

export type Filter =
  | { key: string; label: string; kind: "select"; options: readonly string[] }
  | { key: string; label: string; kind: "text"; placeholder?: string };

export type Column<Row> = {
  header: string;
  cell: (row: Row) => React.ReactNode;
};

/** A filterable table page (FR-161): filters -> query -> table, with every load state handled. */
export function ListPage<Row>({
  title,
  description,
  queryKey,
  filters,
  fetch,
  columns,
  rowKey,
  emptyText,
}: {
  title: string;
  description?: string;
  queryKey: string;
  filters: readonly Filter[];
  fetch: (values: Record<string, string | undefined>) => Promise<Row[]>;
  columns: readonly Column<Row>[];
  rowKey: (row: Row) => string;
  emptyText?: string;
}) {
  const [values, setValues] = React.useState<Record<string, string>>({});
  const active = Object.fromEntries(
    Object.entries(values).map(([k, v]) => [k, v.trim() || undefined]),
  ) as Record<string, string | undefined>;
  const query = useQuery({ queryKey: [queryKey, active], queryFn: () => fetch(active) });

  return (
    <>
      <PageHeader title={title} description={description} />
      <form
        role="search"
        aria-label={`Filter ${title.toLowerCase()}`}
        onSubmit={(e) => e.preventDefault()}
        className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
      >
        {filters.map((filter) => {
          const id = `filter-${filter.key}`;
          const set = (value: string) => setValues((current) => ({ ...current, [filter.key]: value }));
          return (
            <div key={filter.key}>
              <Label htmlFor={id}>{filter.label}</Label>
              {filter.kind === "select" ? (
                <Select id={id} value={values[filter.key] ?? ""} onChange={(e) => set(e.target.value)}>
                  <option value="">Any</option>
                  {filter.options.map((option) => (
                    <option key={option} value={option}>
                      {humanize(option)}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  id={id}
                  value={values[filter.key] ?? ""}
                  placeholder={filter.placeholder}
                  onChange={(e) => set(e.target.value)}
                />
              )}
            </div>
          );
        })}
      </form>
      <QueryState
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={!query.data?.length}
        emptyText={emptyText ?? "Nothing matches these filters."}
      >
        <Table aria-label={title}>
          <thead>
            <tr>
              {columns.map((column) => (
                <Th key={column.header}>{column.header}</Th>
              ))}
            </tr>
          </thead>
          <tbody>
            {query.data?.map((row) => (
              <tr key={rowKey(row)}>
                {columns.map((column) => (
                  <Td key={column.header}>{column.cell(row)}</Td>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>
      </QueryState>
    </>
  );
}
