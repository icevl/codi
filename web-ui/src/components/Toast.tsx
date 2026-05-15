interface Props {
  text: string;
  kind: "info" | "error";
}

export function Toast({ text, kind }: Props) {
  return <div className={`toast${kind === "error" ? " error" : ""}`}>{text}</div>;
}
