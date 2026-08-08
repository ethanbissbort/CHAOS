namespace Chaos.Shell.Core;

/// <summary>Where a found <c>Chaos.Host.exe</c> came from.</summary>
public enum HostExecutableSource
{
    /// <summary>Not found anywhere.</summary>
    None = 0,

    /// <summary>The path the operator set in Settings.</summary>
    Configured = 1,

    /// <summary>In the shell's own directory — the packaged and portable layout.</summary>
    BesideShell = 2,

    /// <summary>A sibling folder of the shell, as some installers lay it out.</summary>
    NearShell = 3,

    /// <summary>A build output inside a repository checkout.</summary>
    DevelopmentCheckout = 4,
}

/// <summary>What the search for the gateway executable found.</summary>
public sealed record HostExecutableProbe
{
    public static readonly HostExecutableProbe NotSearched = new();

    /// <summary>True once a search has actually run.</summary>
    public bool Searched { get; init; }

    /// <summary>Full path to the executable, or null when it was not found.</summary>
    public string? Path { get; init; }

    public HostExecutableSource Source { get; init; }

    /// <summary>Every path that was tried, in order, for the diagnostic.</summary>
    public IReadOnlyList<string> Candidates { get; init; } = Array.Empty<string>();

    /// <summary>
    /// Set when the operator configured a path that is not there. The search
    /// continues, but this must be shown: a configured path that silently falls
    /// back to a different executable is how the wrong build gets run.
    /// </summary>
    public string? Problem { get; init; }

    public bool Found => Path is not null;
}

/// <summary>
/// Finds <c>Chaos.Host.exe</c> so the shell can start the platform itself when
/// no Windows service is installed.
/// </summary>
/// <remarks>
/// <para>
/// Search order, and why: the operator's configured path first, because a
/// setting that can be silently overruled by a file that happens to sit beside
/// the shell is not a setting. Then the shell's own directory, which is the
/// packaged and portable layout. Then a sibling folder. Then, last, the build
/// outputs of a repository checkout, so a developer running from Visual Studio
/// gets the same one-double-click experience without configuring anything.
/// </para>
/// <para>
/// The file system is reached through a delegate so the whole order is testable
/// against a set of pretend paths, including the cases that matter most: a
/// configured path that has gone missing, and a checkout with both Debug and
/// Release outputs present.
/// </para>
/// </remarks>
public static class HostExecutableLocator
{
    /// <summary>The gateway executable's file name.</summary>
    public const string FileName = "Chaos.Host.exe";

    /// <summary>How far up from the shell a repository root is looked for.</summary>
    private const int MaximumAncestors = 8;

    /// <summary>
    /// Locates the executable.
    /// </summary>
    /// <param name="configuredPath">The Settings value, or null.</param>
    /// <param name="shellDirectory">The directory holding the shell executable.</param>
    /// <param name="fileExists">File-existence test.</param>
    public static HostExecutableProbe Locate(
        string? configuredPath,
        string shellDirectory,
        Func<string, bool> fileExists)
    {
        ArgumentNullException.ThrowIfNull(fileExists);
        ArgumentException.ThrowIfNullOrWhiteSpace(shellDirectory);

        var candidates = new List<string>();
        string? problem = null;

        var configured = configuredPath?.Trim();
        if (!string.IsNullOrEmpty(configured))
        {
            // A configured directory is accepted as well as a configured file:
            // an operator pointing at the install folder means the obvious thing.
            var asFile = configured.EndsWith(FileName, StringComparison.OrdinalIgnoreCase)
                ? configured
                : Path.Combine(configured, FileName);

            candidates.Add(asFile);
            if (fileExists(asFile))
            {
                return new HostExecutableProbe
                {
                    Searched = true,
                    Path = asFile,
                    Source = HostExecutableSource.Configured,
                    Candidates = candidates,
                };
            }

            problem =
                $"The platform executable set in Settings is not there: {asFile}. The shell looked "
                + "in its usual places instead.";
        }

        foreach (var (path, source) in NonConfiguredCandidates(shellDirectory))
        {
            candidates.Add(path);
            if (fileExists(path))
            {
                return new HostExecutableProbe
                {
                    Searched = true,
                    Path = path,
                    Source = source,
                    Candidates = candidates,
                    Problem = problem,
                };
            }
        }

        return new HostExecutableProbe
        {
            Searched = true,
            Path = null,
            Source = HostExecutableSource.None,
            Candidates = candidates,
            Problem = problem,
        };
    }

    private static IEnumerable<(string Path, HostExecutableSource Source)> NonConfiguredCandidates(
        string shellDirectory)
    {
        yield return (Path.Combine(shellDirectory, FileName), HostExecutableSource.BesideShell);
        yield return (Path.Combine(shellDirectory, "host", FileName), HostExecutableSource.NearShell);
        yield return (Path.Combine(shellDirectory, "..", "Chaos.Host", FileName), HostExecutableSource.NearShell);

        // Development layouts. Debug before Release: someone running the shell
        // out of a checkout is debugging, and silently attaching to a stale
        // Release build would be the wrong answer for the wrong reason.
        var ancestor = shellDirectory;
        for (var depth = 0; depth < MaximumAncestors; depth++)
        {
            var parent = Path.GetDirectoryName(ancestor);
            if (string.IsNullOrEmpty(parent) || string.Equals(parent, ancestor, StringComparison.Ordinal))
            {
                yield break;
            }

            ancestor = parent;

            foreach (var configuration in new[] { "Debug", "Release" })
            {
                yield return (
                    Path.Combine(ancestor, "windows", "src", "Chaos.Host", "bin", configuration, "net10.0", FileName),
                    HostExecutableSource.DevelopmentCheckout);
                yield return (
                    Path.Combine(ancestor, "src", "Chaos.Host", "bin", configuration, "net10.0", FileName),
                    HostExecutableSource.DevelopmentCheckout);
            }
        }
    }

    /// <summary>One line for the launcher's executable check.</summary>
    public static string Describe(HostExecutableProbe probe)
    {
        ArgumentNullException.ThrowIfNull(probe);

        if (!probe.Searched)
        {
            return "Not looked for yet.";
        }

        if (!probe.Found)
        {
            var where = probe.Candidates.Count == 0
                ? string.Empty
                : $" Looked in {probe.Candidates.Count} places, starting with {probe.Candidates[0]}.";

            return probe.Problem is { Length: > 0 }
                ? probe.Problem + where
                : $"{FileName} was not found, so this shell cannot start the platform itself. Set its "
                  + "location in Settings, or install the platform as a Windows service." + where;
        }

        var source = probe.Source switch
        {
            HostExecutableSource.Configured => "the location set in Settings",
            HostExecutableSource.BesideShell => "the shell's own folder",
            HostExecutableSource.NearShell => "a folder beside the shell",
            HostExecutableSource.DevelopmentCheckout => "a development build in this checkout",
            _ => "an unexpected place",
        };

        var found = $"Found in {source}: {probe.Path}";
        return probe.Problem is { Length: > 0 } ? $"{probe.Problem} {found}" : found;
    }
}
