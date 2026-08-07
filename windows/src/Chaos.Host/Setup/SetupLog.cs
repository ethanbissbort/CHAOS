using System.Globalization;
using System.Text;
using Chaos.Host.Configuration;

namespace Chaos.Host.Setup;

/// <summary>
/// The setup transcript: every command, its full output and its exit code.
/// </summary>
/// <remarks>
/// <c>GET /host/setup</c> names this file on failure, so an operator has
/// somewhere to look that is not a stack trace. Writing is best effort and
/// never throws — a gateway that cannot open its log still has to set the
/// platform up and still has to report what happened.
/// </remarks>
internal interface ISetupLog
{
    /// <summary>Where the transcript is, or null when none could be opened.</summary>
    string? Path { get; }

    /// <summary>Why no transcript could be opened, or null when there is no problem.</summary>
    string? Problem { get; }

    /// <summary>Appends one timestamped line. Never throws.</summary>
    /// <param name="line">The line.</param>
    void Write(string line);
}

/// <summary>Appends to a file, creating it and its directory on first write.</summary>
internal sealed class FileSetupLog : ISetupLog
{
    private readonly Lock _gate = new();
    private readonly string _path;
    private bool _opened;
    private string? _problem;

    /// <summary>Creates a log at <paramref name="path"/>. Nothing is created until the first write.</summary>
    /// <param name="path">Absolute path to the transcript.</param>
    public FileSetupLog(string path) => _path = path;

    /// <inheritdoc/>
    public string? Path => _problem is null ? _path : null;

    /// <inheritdoc/>
    public string? Problem => _problem;

    /// <inheritdoc/>
    public void Write(string line)
    {
        lock (_gate)
        {
            if (_problem is not null)
            {
                return;
            }

            try
            {
                if (!_opened)
                {
                    var directory = System.IO.Path.GetDirectoryName(_path);
                    if (!string.IsNullOrEmpty(directory))
                    {
                        Directory.CreateDirectory(directory);
                    }

                    _opened = true;
                }

                var stamped = string.Create(
                    CultureInfo.InvariantCulture,
                    $"{DateTimeOffset.UtcNow:O}  {line}{Environment.NewLine}");
                File.AppendAllText(_path, stamped, Encoding.UTF8);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException
                                       or NotSupportedException or ArgumentException)
            {
                // Recorded once and surfaced on /host/setup. Never retried in a
                // loop: a full or read-only disk must not turn setup into a
                // spin.
                _problem = $"Could not write the setup log at '{_path}': {ex.GetType().Name}: {ex.Message}";
            }
        }
    }
}

/// <summary>A log that keeps nothing and says so.</summary>
internal sealed class NullSetupLog : ISetupLog
{
    /// <summary>The shared instance.</summary>
    public static NullSetupLog Instance { get; } = new();

    /// <inheritdoc/>
    public string? Path => null;

    /// <inheritdoc/>
    public string? Problem => "No setup log is configured on this host.";

    /// <inheritdoc/>
    public void Write(string line)
    {
        // Deliberately nothing.
    }
}

/// <summary>Resolves where the setup transcript lives.</summary>
internal static class SetupLogFactory
{
    /// <summary>The file name used under the derived directory.</summary>
    public const string FileName = "chaos-setup.log";

    /// <summary>
    /// Builds the log from configuration, or from a per-user state directory
    /// when <see cref="ChaosHostOptions.SetupLogPath"/> is empty.
    /// </summary>
    /// <param name="options">Gateway options.</param>
    /// <returns>The log.</returns>
    /// <remarks>
    /// The derived location is under
    /// <see cref="Environment.SpecialFolder.LocalApplicationData"/> — writable
    /// by the account the gateway runs as, unlike a folder under Program Files.
    /// </remarks>
    public static ISetupLog Create(ChaosHostOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);

        if (!string.IsNullOrWhiteSpace(options.SetupLogPath))
        {
            return new FileSetupLog(System.IO.Path.GetFullPath(options.SetupLogPath));
        }

        var directory = DerivedStateDirectory();
        return directory is null
            ? NullSetupLog.Instance
            : new FileSetupLog(System.IO.Path.Combine(directory, FileName));
    }

    /// <summary>
    /// The per-user directory the gateway keeps setup state in, or null when
    /// the platform does not give us one.
    /// </summary>
    /// <returns>An absolute directory path, or null.</returns>
    public static string? DerivedStateDirectory()
    {
        var root = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return string.IsNullOrWhiteSpace(root)
            ? null
            : System.IO.Path.Combine(root, "Project CHAOS");
    }
}
