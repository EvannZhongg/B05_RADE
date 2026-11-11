import type { JsonInput } from "@rade/protocol";

export async function readJsonInput(filename: string, read: () => Promise<ArrayBuffer | Uint8Array>): Promise<JsonInput> {
  if (!/\.json$/i.test(filename)) throw new Error("请选择 .json 文件。");
  const bytes = await read();
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    JSON.parse(text);
  } catch {
    throw new Error("文件内容不是有效的 UTF-8 JSON。");
  }
  return { filename, text };
}
