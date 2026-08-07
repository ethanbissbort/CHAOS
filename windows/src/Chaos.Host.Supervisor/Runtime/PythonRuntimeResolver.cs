namespace Chaos.Host.Supervisor.Runtime;

/// <summary>Finds the Python that will run the platform.</summary>
public interface IPythonRuntimeResolver
{
    BackendRuntimeResolution Resolve(BackendSupervisorOptions options);
}

/// <summary>
/// Resolves the EMBEDDED runtime first, the DEVELOPMENT layout second.
/// </summary>
/// <remarks>
/// <para>
/// The one rule that matters: a <em>present but broken</em> embedded install is
/// a hard failure. If <c>&lt;InstallRoot&gt;\python\</c> exists and the
/// interpreter inside it does not, we do not quietly borrow the machine's
/// Python — that turns "the installer is corrupt" into "it works on my machine
/// and mysteriously not on the customer's", which is exactly the class of bug
/// that gets found at 3am with the battery at 20%.
/// </para>
/// <para>
/// Falling through from "no embedded runtime at all" to development is
/// legitimate (that is a checkout, not a broken install) and is always reported
/// in <see cref="BackendRuntimeResolution.Detail"/>.
/// </para>
/// </remarks>
public sealed class PythonRuntimeResolver : IPythonRuntimeResolver
{
    /// <summary>Directory under the install root holding the shipped runtime.</summary>
    public const string EmbeddedRuntimeDirectoryName = "python";

    /// <summary>Levels walked up from the install root looking for a checkout.</summary>
    private const int RepositorySearchDepth = 8;

    private static readonly string[] WindowsEmbeddedInterpreters = ["python.exe", "pythonw.exe"];
    private static readonly string[] UnixEmbeddedInterpreters = ["bin/python3", "bin/python", "python3", "python"];
    private static readonly string[] PathInterpreterNames = ["python3", "python"];

    private readonly IFileSystem _fileSystem;
    private readonly bool _isWindows;

    public PythonRuntimeResolver(IFileSystem fileSystem)
        : this(fileSystem, OperatingSystem.IsWindows())
    {
    }

    // The platform flag is injectable so the Windows layout rules are testable
    // from the Linux CI leg. Nothing else in this class asks what OS it is on.
    internal PythonRuntimeResolver(IFileSystem fileSystem, bool isWindows)
    {
        _fileSystem = fileSystem ?? throw new ArgumentNullException(nameof(fileSystem));
        _isWindows = isWindows;
    }

    public BackendRuntimeResolution Resolve(BackendSupervisorOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);

        var attempts = new List<string>();
        var installRoot = options.InstallRoot is { Length: > 0 }
            ? options.InstallRoot
            : AppContext.BaseDirectory;

        var embedded = TryEmbedded(options, installRoot, attempts, out var brokenInstall);
        if (embedded is not null)
        {
            return BackendRuntimeResolution.Success(embedded, attempts);
        }

        if (brokenInstall is not null)
        {
            return BackendRuntimeResolution.Failure(brokenInstall, attempts);
        }

        if (options.RequireEmbeddedRuntime)
        {
            return BackendRuntimeResolution.Failure(
                $"No embedded Python runtime under '{EmbeddedRoot(installRoot)}' and " +
                $"{nameof(BackendSupervisorOptions.RequireEmbeddedRuntime)} is set, so the development " +
                "layout was not tried. This install is incomplete.",
                attempts);
        }

        var development = TryDevelopment(options, installRoot, attempts, out var developmentFailure);
        if (development is not null)
        {
            return BackendRuntimeResolution.Success(development, attempts);
        }

        return BackendRuntimeResolution.Failure(
            developmentFailure ??
            "No Python runtime could be resolved in either the embedded or the development layout.",
            attempts);
    }

    // -- Embedded ----------------------------------------------------------

    private BackendRuntimeDescriptor? TryEmbedded(
        BackendSupervisorOptions options,
        string installRoot,
        List<string> attempts,
        out string? brokenInstall)
    {
        brokenInstall = null;
        var embeddedRoot = EmbeddedRoot(installRoot);

        if (!_fileSystem.DirectoryExists(embeddedRoot))
        {
            attempts.Add($"embedded: no '{embeddedRoot}' directory — this is not a packaged install");
            return null;
        }

        var candidates = _isWindows ? WindowsEmbeddedInterpreters : UnixEmbeddedInterpreters;
        string? executable = null;
        foreach (var candidate in candidates)
        {
            var path = Path.Combine(embeddedRoot, candidate.Replace('/', Path.DirectorySeparatorChar));
            if (_fileSystem.FileExists(path))
            {
                executable = path;
                attempts.Add($"embedded: found interpreter '{path}'");
                break;
            }

            attempts.Add($"embedded: no '{path}'");
        }

        if (executable is null)
        {
            // Present but broken. Stop here on purpose.
            brokenInstall =
                $"The embedded runtime directory '{embeddedRoot}' exists but contains no interpreter " +
                $"({string.Join(", ", candidates)}). This install is damaged. The supervisor will not " +
                "fall back to a system Python, because that would hide the damage and run the platform " +
                "against unknown dependency versions.";
            return null;
        }

        // The packaged layout ships the platform source beside the runtime.
        // If it is not there, the package is expected to be installed into the
        // embedded interpreter's own site-packages.
        var environment = new Dictionary<string, string?>(StringComparer.Ordinal);
        var appRoot = Path.Combine(installRoot, "app");
        var appSource = Path.Combine(appRoot, "src");
        string workingDirectory;
        string sourceNote;

        if (_fileSystem.FileExists(CliPath(appSource)))
        {
            environment["PYTHONPATH"] = appSource;
            workingDirectory = appRoot;
            sourceNote = $"platform source at '{appSource}' (PYTHONPATH)";
        }
        else
        {
            workingDirectory = installRoot;
            sourceNote = "homestead_twin expected in the embedded interpreter's site-packages";
            attempts.Add($"embedded: no '{CliPath(appSource)}', assuming an installed package");
        }

        return new BackendRuntimeDescriptor
        {
            Layout = BackendRuntimeLayout.Embedded,
            Executable = executable,
            BaseArguments = [],
            WorkingDirectory = workingDirectory,
            Environment = environment,
            Description = $"EMBEDDED runtime '{executable}', {sourceNote}",
        };
    }

    // -- Development -------------------------------------------------------

    private BackendRuntimeDescriptor? TryDevelopment(
        BackendSupervisorOptions options,
        string installRoot,
        List<string> attempts,
        out string? failure)
    {
        failure = null;

        var repositoryRoot = ResolveRepositoryRoot(options, installRoot, attempts, out var repositoryFailure);
        if (repositoryRoot is null)
        {
            failure = repositoryFailure;
            return null;
        }

        var executable = ResolveDevelopmentInterpreter(options, attempts, out var interpreterFailure);
        if (executable is null)
        {
            failure = interpreterFailure;
            return null;
        }

        var source = Path.Combine(repositoryRoot, "src");
        return new BackendRuntimeDescriptor
        {
            Layout = BackendRuntimeLayout.Development,
            Executable = executable,
            BaseArguments = [],
            WorkingDirectory = repositoryRoot,
            Environment = new Dictionary<string, string?>(StringComparer.Ordinal)
            {
                ["PYTHONPATH"] = source,
            },
            Description = $"DEVELOPMENT layout: interpreter '{executable}', PYTHONPATH '{source}'",
        };
    }

    private string? ResolveRepositoryRoot(
        BackendSupervisorOptions options,
        string installRoot,
        List<string> attempts,
        out string? failure)
    {
        failure = null;

        if (options.RepositoryRoot is { Length: > 0 } configured)
        {
            if (_fileSystem.FileExists(CliPath(Path.Combine(configured, "src"))))
            {
                attempts.Add($"development: configured repository root '{configured}' contains the platform source");
                return configured;
            }

            attempts.Add($"development: configured repository root '{configured}' has no '{CliPath(Path.Combine(configured, "src"))}'");
            failure =
                $"{nameof(BackendSupervisorOptions.RepositoryRoot)} is set to '{configured}' but there is no " +
                $"'{CliPath(Path.Combine(configured, "src"))}' under it. Configured explicitly, so no search was made.";
            return null;
        }

        var directory = installRoot;
        for (var level = 0; level < RepositorySearchDepth && !string.IsNullOrEmpty(directory); level++)
        {
            var cli = CliPath(Path.Combine(directory, "src"));
            if (_fileSystem.FileExists(cli))
            {
                attempts.Add($"development: found the platform source at '{cli}'");
                return directory;
            }

            directory = Path.GetDirectoryName(directory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        }

        attempts.Add(
            $"development: walked {RepositorySearchDepth.ToString(System.Globalization.CultureInfo.InvariantCulture)} " +
            $"levels up from '{installRoot}' without finding src/homestead_twin/cli.py");
        failure =
            "No platform source found. There is no embedded runtime and no checkout above " +
            $"'{installRoot}' containing src/homestead_twin/cli.py. Set " +
            $"{nameof(BackendSupervisorOptions.RepositoryRoot)} or install the packaged runtime.";
        return null;
    }

    private string? ResolveDevelopmentInterpreter(
        BackendSupervisorOptions options,
        List<string> attempts,
        out string? failure)
    {
        failure = null;

        if (options.PythonExecutable is { Length: > 0 } configured)
        {
            if (_fileSystem.FileExists(configured))
            {
                attempts.Add($"development: configured interpreter '{configured}'");
                return configured;
            }

            var onPath = _fileSystem.FindOnPath(configured);
            if (onPath is not null)
            {
                attempts.Add($"development: configured interpreter '{configured}' resolved on PATH to '{onPath}'");
                return onPath;
            }

            attempts.Add($"development: configured interpreter '{configured}' does not exist and is not on PATH");
            failure =
                $"{nameof(BackendSupervisorOptions.PythonExecutable)} is set to '{configured}', which is neither " +
                "a file nor on PATH. Configured explicitly, so no other interpreter was tried.";
            return null;
        }

        foreach (var name in PathInterpreterNames)
        {
            var found = _fileSystem.FindOnPath(name);
            if (found is not null)
            {
                attempts.Add($"development: '{name}' resolved on PATH to '{found}'");
                return found;
            }

            attempts.Add($"development: '{name}' not on PATH");
        }

        failure =
            $"No Python interpreter on PATH ({string.Join(", ", PathInterpreterNames)}) and no embedded runtime. " +
            $"Install Python 3.11+ or set {nameof(BackendSupervisorOptions.PythonExecutable)}.";
        return null;
    }

    private static string EmbeddedRoot(string installRoot) =>
        Path.Combine(installRoot, EmbeddedRuntimeDirectoryName);

    private static string CliPath(string sourceRoot) =>
        Path.Combine(sourceRoot, "homestead_twin", "cli.py");
}
