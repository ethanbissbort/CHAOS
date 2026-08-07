using System.Net;
using System.Text;

namespace Chaos.Host.Docs;

/// <summary>
/// The two pages the documentation listener serves itself: "not generated yet"
/// and "no such page".
/// </summary>
/// <remarks>
/// <para>
/// Both are plain, self-contained HTML with no external stylesheet, no font and
/// no script. The whole point of this listener is that it works with the
/// internet gone, the backend dead and the database unreadable, so a page it
/// generates itself must not depend on anything either.
/// </para>
/// <para>
/// Neither page is ever an exception page. An operator reading the manual during
/// an outage is having a bad enough evening without a stack trace.
/// </para>
/// </remarks>
internal static class DocumentationPages
{
    /// <summary>
    /// Marks a response the listener generated itself rather than served from
    /// the site, so a tool can tell the difference without parsing HTML.
    /// </summary>
    public const string StateHeader = "X-Chaos-Docs";

    /// <summary>Header value when no site has been generated.</summary>
    public const string StateNotGenerated = "not-generated";

    /// <summary>Header value when the site exists but the path does not.</summary>
    public const string StateNotFound = "page-not-found";

    /// <summary>
    /// The page every request gets when no site has been generated. HTTP 200,
    /// on purpose: nothing is broken, the manual simply has not been built yet,
    /// and a 404 would send the reader hunting for a misconfigured URL.
    /// </summary>
    /// <param name="root">Where the gateway looked for the site.</param>
    /// <returns>A complete HTML document.</returns>
    public static string NotGenerated(DocumentationRootResolution root)
    {
        var searched = root.SearchedPaths.Count == 0
            ? "<li><em>nothing — no candidate path was even computed</em></li>"
            : string.Concat(root.SearchedPaths.Select(path => $"<li><code>{Encode(path)}</code></li>"));

        var found = root.Path is null
            ? "<p>No documentation directory was found. The gateway looked in these places, in order:</p>"
            : $"<p>The directory <code>{Encode(root.Path)}</code> exists but has no "
            + $"<code>{DocumentationRootResolution.IndexFileName}</code> in it, so the site was never finished "
            + "being generated. The gateway looked in these places, in order:</p>";

        return Document(
            title: "Project CHAOS documentation — not built yet",
            heading: "The documentation site has not been built yet",
            body: new StringBuilder()
                .Append("<p>This listener serves the offline copy of the Project CHAOS manuals: the same "
                      + "Markdown that lives in <code>docs/</code> in the repository, rendered into a site that "
                      + "needs no internet, no backend and no database. It has not been generated on this "
                      + "machine yet.</p>")
                .Append("<h2>Build it from Visual Studio</h2>")
                .Append("<ol>")
                .Append("<li>Open <code>windows\\CHAOS.sln</code> in Visual Studio 2026.</li>")
                .Append("<li>In <strong>Solution Explorer</strong>, right-click the <strong>Chaos.Runtime</strong> "
                      + "project (under the <em>build</em> folder) and choose <strong>Build</strong>. "
                      + "<em>Build &gt; Build Solution</em> does the same thing.</li>")
                .Append("<li>Reload this page. The documentation build is incremental: it only re-runs when a "
                      + "<code>.md</code> file has changed.</li>")
                .Append("</ol>")
                .Append("<p>If the build reports that no Python interpreter was found, build the embedded "
                      + "Python runtime first — that is the same <strong>Chaos.Runtime</strong> project, and it "
                      + "is what <em>Rebuild</em> on that project forces. No command prompt is needed for any of "
                      + "this.</p>")
                .Append("<h2>Where the gateway looked</h2>")
                .Append(found)
                .Append("<ul>").Append(searched).Append("</ul>")
                .Append("<p class=\"muted\">The Markdown sources are readable as-is in the meantime: they are "
                      + "in <code>docs/</code> in the repository, and on an installed machine in the "
                      + "documentation folder beside the operator console assets.</p>")
                .ToString());
    }

    /// <summary>
    /// The page an unknown path gets when a site <em>has</em> been generated.
    /// </summary>
    /// <param name="path">The path that was requested.</param>
    /// <returns>A complete HTML document.</returns>
    public static string NotFound(string path) => Document(
        title: "Project CHAOS documentation — page not found",
        heading: "No such page in the documentation",
        body: $"<p>The documentation site is installed and serving, but it has no page at "
            + $"<code>{Encode(path)}</code>.</p>"
            + "<p><a href=\"/\">Go to the contents page</a>.</p>"
            + "<p class=\"muted\">If a page you expect is missing, rebuild the documentation: right-click "
            + "<strong>Chaos.Runtime</strong> in Solution Explorer and choose <strong>Build</strong>.</p>");

    // Two '$' so that a single brace is a literal brace: this document is mostly
    // CSS, and CSS is nothing but braces.
    private static string Document(string title, string heading, string body) =>
        $$"""
          <!doctype html>
          <html lang="en">
          <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{{Encode(title)}}</title>
          <style>
          :root { color-scheme: light dark; }
          body { margin: 0 auto; padding: 2rem 1.25rem 4rem; max-width: 46rem; line-height: 1.55;
                 font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }
          h1 { font-size: 1.5rem; line-height: 1.25; }
          h2 { font-size: 1.1rem; margin-top: 2rem; }
          code { font-family: Consolas, "SF Mono", ui-monospace, monospace; font-size: 0.95em;
                 padding: 0.1em 0.3em; border-radius: 3px; background: rgba(127,127,127,0.18); }
          li { margin: 0.35rem 0; }
          .muted { opacity: 0.75; font-size: 0.95rem; }
          .brand { font-size: 0.8rem; letter-spacing: 0.14em; text-transform: uppercase; opacity: 0.7; }
          </style>
          </head>
          <body>
          <p class="brand">Project CHAOS &middot; documentation</p>
          <h1>{{Encode(heading)}}</h1>
          {{body}}
          </body>
          </html>
          """;

    private static string Encode(string value) => WebUtility.HtmlEncode(value);
}
