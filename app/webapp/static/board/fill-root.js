/* `<root>` in an owner step: the one placeholder fleet-config's owner_check
 * notes hold for this PC's fleet root, which public text never spells out.
 * Only the clipboard gets the real folder; the card keeps the placeholder.
 *
 * Pure, with no imports, so plain Node can pin it.
 */

const ROOT = '<root>';

// `{ text, unfilled }`: `text` with every `<root>` replaced by `root`. With no
// root the text comes back as it was, `unfilled` when it names the placeholder.
export function fillRoot(text, root) {
  if (text.indexOf(ROOT) === -1) return { text: text, unfilled: false };
  if (!root) return { text: text, unfilled: true };
  // A drive root ends in a separator, which the command already writes.
  const bare = root.replace(/[\\/]+$/, '');
  return {
    // Replacer functions: a `$` in a folder name is not a pattern.
    text: text.replace(/<root>(?=[\\/])/g, function () { return bare; })
      .replaceAll(ROOT, function () { return root; }),
    unfilled: false,
  };
}
