using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace Chaos.Host.Setup;

/// <summary>
/// What is on disk in <c>data/</c>: whether it is there, how many design
/// documents it holds, and a fingerprint of their contents.
/// </summary>
/// <param name="Directory">The directory inspected, or null when none is known.</param>
/// <param name="Source">How the directory was arrived at: <c>configured</c>, <c>derived</c> or <c>unknown</c>.</param>
/// <param name="Exists">Whether the directory exists.</param>
/// <param name="DocumentCount">How many YAML design documents it holds, or null when the directory is not known.</param>
/// <param name="Fingerprint">
/// A SHA-256 over the names and contents of those documents, or null when it
/// could not be computed. Two identical fingerprints mean the same design
/// package; different ones mean it changed.
/// </param>
/// <param name="Problem">Why the inspection could not complete, or null.</param>
internal sealed record DesignPackageFacts(
    string? Directory,
    string Source,
    bool Exists,
    int? DocumentCount,
    string? Fingerprint,
    string? Problem)
{
    /// <summary>Nothing is known about the design package.</summary>
    /// <param name="reason">Why nothing is known.</param>
    /// <returns>The facts.</returns>
    public static DesignPackageFacts Unknown(string reason) =>
        new(null, "unknown", Exists: false, DocumentCount: null, Fingerprint: null, Problem: reason);

    /// <summary>
    /// Inspects a design-package directory, hashing every <c>*.yaml</c> in it.
    /// </summary>
    /// <param name="directory">The directory, or null.</param>
    /// <param name="source">Where the path came from: <c>configured</c> or <c>derived</c>.</param>
    /// <returns>The facts. Never throws.</returns>
    /// <remarks>
    /// YAML only: <c>data/</c> also ships JSON mirrors of every document, and
    /// the registry loader reads the YAML. Hashing both would report a change
    /// twice.
    /// </remarks>
    public static DesignPackageFacts Inspect(string? directory, string source)
    {
        if (string.IsNullOrWhiteSpace(directory))
        {
            return Unknown(
                "No design-package directory is configured and none could be derived from the resolved Python "
              + "runtime, so the gateway does not know where data/ is on this machine.");
        }

        string full;
        try
        {
            full = Path.GetFullPath(directory);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return new DesignPackageFacts(directory, source, Exists: false, DocumentCount: null, Fingerprint: null,
                Problem: $"'{directory}' is not a usable path: {ex.Message}");
        }

        if (!System.IO.Directory.Exists(full))
        {
            return new DesignPackageFacts(full, source, Exists: false, DocumentCount: 0, Fingerprint: null,
                Problem: null);
        }

        try
        {
            var files = System.IO.Directory.GetFiles(full, "*.yaml", SearchOption.TopDirectoryOnly);
            Array.Sort(files, StringComparer.Ordinal);

            if (files.Length == 0)
            {
                return new DesignPackageFacts(full, source, Exists: true, DocumentCount: 0, Fingerprint: null,
                    Problem: null);
            }

            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            foreach (var file in files)
            {
                hash.AppendData(Encoding.UTF8.GetBytes(Path.GetFileName(file)));
                hash.AppendData([0]);
                hash.AppendData(File.ReadAllBytes(file));
                hash.AppendData([0]);
            }

            var digest = Convert.ToHexStringLower(hash.GetHashAndReset());
            return new DesignPackageFacts(full, source, Exists: true, DocumentCount: files.Length,
                Fingerprint: "sha256:" + digest, Problem: null);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return new DesignPackageFacts(full, source, Exists: true, DocumentCount: null, Fingerprint: null,
                Problem: $"Could not read '{full}': {ex.GetType().Name}: {ex.Message}");
        }
    }
}

/// <summary>
/// What can be known about the database file without opening it.
/// </summary>
/// <param name="Url">The URL as the platform reported it, or as configured. Null when unknown.</param>
/// <param name="FilePath">The file path, when the URL names a SQLite file. Null otherwise.</param>
/// <param name="FileExists">Whether that file exists, or null when the URL is not a file URL.</param>
/// <param name="FileSizeBytes">Its size, or null.</param>
internal sealed record DatabaseFileFacts(
    string? Url,
    string? FilePath,
    bool? FileExists,
    long? FileSizeBytes)
{
    private const string SqlitePrefix = "sqlite:///";

    /// <summary>Nothing is known about the database.</summary>
    public static DatabaseFileFacts Unknown { get; } = new(null, null, null, null);

    /// <summary>Whether this is a SQLite file database.</summary>
    public bool IsFile => FilePath is not null;

    /// <summary>Inspects the file behind a SQLAlchemy URL, if there is one.</summary>
    /// <param name="url">The URL, or null.</param>
    /// <returns>The facts. Never throws.</returns>
    /// <remarks>
    /// Only SQLite file URLs have a file. A PostgreSQL URL has a server, and
    /// the gateway does not go looking for one — <c>status --json</c> already
    /// says whether the platform could reach it.
    /// </remarks>
    public static DatabaseFileFacts Inspect(string? url)
    {
        if (string.IsNullOrWhiteSpace(url))
        {
            return Unknown;
        }

        if (!url.StartsWith(SqlitePrefix, StringComparison.OrdinalIgnoreCase))
        {
            return new DatabaseFileFacts(url, null, null, null);
        }

        var raw = url[SqlitePrefix.Length..];
        if (raw.Length == 0 || string.Equals(raw, ":memory:", StringComparison.Ordinal))
        {
            return new DatabaseFileFacts(url, null, null, null);
        }

        try
        {
            var info = new FileInfo(Path.GetFullPath(raw));
            return info.Exists
                ? new DatabaseFileFacts(url, info.FullName, true, info.Length)
                : new DatabaseFileFacts(url, info.FullName, false, null);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException
                                   or PathTooLongException or IOException or UnauthorizedAccessException)
        {
            return new DatabaseFileFacts(url, raw, null, null);
        }
    }

    /// <summary>A sentence describing the file, for the <c>database</c> step.</summary>
    /// <returns>The description.</returns>
    public string Describe()
    {
        if (Url is null)
        {
            return "The platform has not reported which database it uses.";
        }

        if (!IsFile)
        {
            return $"The platform is configured against '{Url}'. That is not a local file, so the gateway "
                 + "reports what the platform says about it rather than inspecting a path.";
        }

        return FileExists switch
        {
            true => string.Create(
                CultureInfo.InvariantCulture,
                $"SQLite database file '{FilePath}' exists ({FileSizeBytes ?? 0} bytes)."),
            false => $"SQLite database file '{FilePath}' does not exist yet. It is created the first time the "
                   + "platform opens it.",
            _ => $"SQLite database file '{FilePath}' could not be inspected.",
        };
    }
}
