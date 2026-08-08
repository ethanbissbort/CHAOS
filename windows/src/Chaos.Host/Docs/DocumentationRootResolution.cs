namespace Chaos.Host.Docs;

/// <summary>
/// Where the generated documentation site was found, or where the gateway
/// looked and failed.
/// </summary>
/// <param name="Path">The resolved absolute directory, or null when nothing was found.</param>
/// <param name="Source">How it was resolved: <c>configured</c>, <c>web-root</c>, <c>content-root</c>, <c>app-directory</c>, <c>repository</c>, or <c>not-found</c>.</param>
/// <param name="SearchedPaths">Every candidate that was probed, in order. Reported on <c>/host/info</c> so an absent manual is diagnosable without guessing.</param>
public sealed record DocumentationRootResolution(string? Path, string Source, IReadOnlyList<string> SearchedPaths)
{
    /// <summary>The file the site is entered through when it is present.</summary>
    public const string IndexFileName = "index.html";

    /// <summary>Whether a directory was found.</summary>
    public bool Present => Path is not null;

    /// <summary>
    /// Whether the directory actually contains a generated site, rather than
    /// just existing.
    /// </summary>
    /// <remarks>
    /// An empty <c>docs</c> directory left behind by a half-finished generator
    /// run must read as "not generated", not as a site that 404s on every page.
    /// </remarks>
    public bool HasIndex => Path is not null && File.Exists(System.IO.Path.Combine(Path, IndexFileName));
}
