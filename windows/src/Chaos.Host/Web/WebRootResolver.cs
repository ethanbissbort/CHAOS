using Chaos.Host.Configuration;

namespace Chaos.Host.Web;

/// <summary>
/// Finds the operator console assets (<c>src/homestead_twin/web/</c>).
/// </summary>
/// <remarks>
/// <para>
/// The browser interface is a hosted service of this gateway, not an optional
/// extra, so the resolver probes the plausible layouts rather than requiring
/// configuration on every machine:
/// </para>
/// <list type="number">
/// <item><description><c>Chaos:WebRootPath</c>, absolute or relative to the content root.</description></item>
/// <item><description><c>&lt;content root&gt;/web</c> — the packaged layout, assets copied next to the executable.</description></item>
/// <item><description><c>&lt;executable directory&gt;/web</c> — when the content root differs, e.g. a Windows Service started from <c>C:\Windows\System32</c>.</description></item>
/// <item><description><c>src/homestead_twin/web</c> found by walking up from the content root — the development repository layout.</description></item>
/// </list>
/// <para>
/// Every candidate is recorded. When nothing is found the result says so
/// explicitly and lists what was tried; it never invents a path.
/// </para>
/// </remarks>
public static class WebRootResolver
{
    /// <summary>Relative path to the console assets inside the Python repository.</summary>
    public const string RepositoryRelativePath = "src/homestead_twin/web";

    private const int MaxAncestorLevels = 8;

    /// <summary>Resolves the operator console directory.</summary>
    /// <param name="options">Gateway options, for <see cref="ChaosHostOptions.WebRootPath"/>.</param>
    /// <param name="contentRootPath">The host's content root.</param>
    /// <param name="appDirectory">The directory the assembly was loaded from. Defaults to <see cref="AppContext.BaseDirectory"/>.</param>
    /// <returns>The resolution, including every path probed.</returns>
    public static WebRootResolution Resolve(ChaosHostOptions options, string contentRootPath, string? appDirectory = null)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(contentRootPath);

        appDirectory ??= AppContext.BaseDirectory;
        var searched = new List<string>();

        if (!string.IsNullOrWhiteSpace(options.WebRootPath))
        {
            var configured = Path.GetFullPath(options.WebRootPath, contentRootPath);
            searched.Add(configured);

            // A configured path that does not exist is reported as not found
            // rather than silently falling back to a probe: someone stated an
            // intent, and quietly serving different files would be worse than
            // serving none.
            return Directory.Exists(configured)
                ? new WebRootResolution(configured, "configured", searched)
                : new WebRootResolution(null, "not-found", searched);
        }

        if (TryCandidate(Path.Combine(contentRootPath, "web"), searched, out var packaged))
        {
            return new WebRootResolution(packaged, "content-root", searched);
        }

        if (TryCandidate(Path.Combine(appDirectory, "web"), searched, out var beside))
        {
            return new WebRootResolution(beside, "app-directory", searched);
        }

        var directory = new DirectoryInfo(Path.GetFullPath(contentRootPath));
        for (var level = 0; level < MaxAncestorLevels && directory is not null; level++, directory = directory.Parent)
        {
            var candidate = Path.Combine(directory.FullName, RepositoryRelativePath.Replace('/', Path.DirectorySeparatorChar));
            if (TryCandidate(candidate, searched, out var repository))
            {
                return new WebRootResolution(repository, "repository", searched);
            }
        }

        return new WebRootResolution(null, "not-found", searched);
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
