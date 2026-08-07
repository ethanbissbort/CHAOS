using System.Runtime.InteropServices;

namespace Chaos.Host.Supervisor.Runtime;

/// <summary>
/// The few filesystem questions runtime resolution asks. Behind an interface so
/// "embedded install is present but broken" can be tested without building a
/// broken install.
/// </summary>
public interface IFileSystem
{
    bool FileExists(string path);

    bool DirectoryExists(string path);

    /// <summary>
    /// Resolves an executable name against PATH (and PATHEXT on Windows).
    /// Returns null when it is not there.
    /// </summary>
    string? FindOnPath(string fileName);
}

/// <summary>The real filesystem.</summary>
public sealed class PhysicalFileSystem : IFileSystem
{
    public static PhysicalFileSystem Instance { get; } = new();

    public bool FileExists(string path) => !string.IsNullOrWhiteSpace(path) && File.Exists(path);

    public bool DirectoryExists(string path) => !string.IsNullOrWhiteSpace(path) && Directory.Exists(path);

    public string? FindOnPath(string fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName))
        {
            return null;
        }

        // An explicit path is not a PATH lookup.
        if (fileName.Contains(Path.DirectorySeparatorChar, StringComparison.Ordinal) ||
            fileName.Contains(Path.AltDirectorySeparatorChar, StringComparison.Ordinal))
        {
            return File.Exists(fileName) ? Path.GetFullPath(fileName) : null;
        }

        var path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path))
        {
            return null;
        }

        var extensions = Extensions();
        foreach (var directory in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            foreach (var extension in extensions)
            {
                string candidate;
                try
                {
                    candidate = Path.Combine(directory, fileName + extension);
                }
                catch (ArgumentException)
                {
                    // A malformed PATH entry is not a reason to stop looking.
                    continue;
                }

                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }

        return null;
    }

    private static string[] Extensions()
    {
        if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            return [string.Empty];
        }

        var pathExt = Environment.GetEnvironmentVariable("PATHEXT");
        if (string.IsNullOrEmpty(pathExt))
        {
            return [".exe", ".cmd", ".bat", string.Empty];
        }

        var parsed = pathExt.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        return [.. parsed, string.Empty];
    }
}
