using Chaos.Host.Configuration;
using Chaos.Host.Web;

namespace Chaos.Host.Docs;

/// <summary>
/// Finds the generated documentation site (<c>src/chaos/web/docs/</c>).
/// </summary>
/// <remarks>
/// <para>
/// The generator writes the site <i>inside</i> the operator console's asset
/// tree, so in every layout the answer is "<c>docs</c> beside the console
/// assets". That is resolved from <see cref="WebRootResolution"/> rather than
/// probed again, so the two can never disagree about which checkout or which
/// install they are looking at.
/// </para>
/// <para>
/// The repository fallbacks exist for the case where the console assets are
/// pinned somewhere unusual, or absent, but the manual has still been built.
/// Every candidate is recorded; when nothing is found the result says so and
/// lists what was tried. It never invents a path.
/// </para>
/// </remarks>
public static class DocumentationRootResolver
{
    /// <summary>The directory name the generator writes into, inside the web root.</summary>
    public const string DirectoryName = "docs";

    /// <summary>Relative path to the generated site inside the Python repository.</summary>
    public const string RepositoryRelativePath = "src/chaos/web/docs";

    private const int MaxAncestorLevels = 8;

    /// <summary>Resolves the documentation site directory.</summary>
    /// <param name="options">Documentation options, for <see cref="DocumentationOptions.RootPath"/>.</param>
    /// <param name="webRoot">Where the operator console assets were resolved to.</param>
    /// <param name="contentRootPath">The host's content root.</param>
    /// <param name="appDirectory">The directory the assembly was loaded from. Defaults to <see cref="AppContext.BaseDirectory"/>.</param>
    /// <returns>The resolution, including every path probed.</returns>
    public static DocumentationRootResolution Resolve(
        DocumentationOptions options,
        WebRootResolution webRoot,
        string contentRootPath,
        string? appDirectory = null)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(webRoot);
        ArgumentNullException.ThrowIfNull(contentRootPath);

        appDirectory ??= AppContext.BaseDirectory;
        var searched = new List<string>();

        if (!string.IsNullOrWhiteSpace(options.RootPath))
        {
            var configured = Path.GetFullPath(options.RootPath, contentRootPath);
            searched.Add(configured);

            // A configured path that does not exist is reported as not found
            // rather than quietly falling back to a probe. Someone stated an
            // intent; serving a different manual than the one they named would
            // be worse than serving the "not generated yet" page.
            return Directory.Exists(configured)
                ? new DocumentationRootResolution(configured, "configured", searched)
                : new DocumentationRootResolution(null, "not-found", searched);
        }

        if (webRoot.Path is not null
            && TryCandidate(Path.Combine(webRoot.Path, DirectoryName), searched, out var besideConsole))
        {
            return new DocumentationRootResolution(besideConsole, "web-root", searched);
        }

        if (TryCandidate(Path.Combine(contentRootPath, "web", DirectoryName), searched, out var packaged))
        {
            return new DocumentationRootResolution(packaged, "content-root", searched);
        }

        if (TryCandidate(Path.Combine(appDirectory, "web", DirectoryName), searched, out var beside))
        {
            return new DocumentationRootResolution(beside, "app-directory", searched);
        }

        var directory = new DirectoryInfo(Path.GetFullPath(contentRootPath));
        for (var level = 0; level < MaxAncestorLevels && directory is not null; level++, directory = directory.Parent)
        {
            var candidate = Path.Combine(
                directory.FullName,
                RepositoryRelativePath.Replace('/', Path.DirectorySeparatorChar));

            if (TryCandidate(candidate, searched, out var repository))
            {
                return new DocumentationRootResolution(repository, "repository", searched);
            }
        }

        return new DocumentationRootResolution(null, "not-found", searched);
    }

    private static bool TryCandidate(string candidate, List<string> searched, out string resolved)
    {
        resolved = Path.GetFullPath(candidate);
        if (searched.Contains(resolved, StringComparer.Ordinal))
        {
            return false;
        }

        searched.Add(resolved);
        return Directory.Exists(resolved);
    }
}
