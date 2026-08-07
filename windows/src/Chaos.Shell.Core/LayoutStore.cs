using System.Text.Json;

namespace Chaos.Shell.Core;

/// <summary>Reads and writes the persisted window layout.</summary>
public interface ILayoutStore
{
    ShellLayout Load();

    /// <summary>
    /// Saves the layout. Returns the failure reason, or null on success.
    /// Never throws: failing to write a cosmetic preference must not take down
    /// the shell, and definitely must not do so on exit.
    /// </summary>
    string? Save(ShellLayout layout);
}

/// <summary>
/// JSON layout store, written atomically next to the shell's other per-user
/// state.
/// </summary>
/// <remarks>
/// Load never throws. A truncated or hand-edited file yields
/// <see cref="ShellLayout.Empty"/> and the shell opens with defaults, because
/// an operator locked out of the console by a corrupt preferences file is a far
/// worse failure than losing a window position.
/// </remarks>
public sealed class FileLayoutStore : ILayoutStore
{
    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
    };

    private readonly string _path;

    public FileLayoutStore(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        _path = path;
    }

    public string Path => _path;

    /// <summary>
    /// <c>%APPDATA%\ProjectCHAOS\Shell\layout.json</c> on Windows; the
    /// equivalent per-user config directory elsewhere.
    /// </summary>
    public static string DefaultPath => System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "ProjectCHAOS",
        "Shell",
        "layout.json");

    public static FileLayoutStore Default() => new(DefaultPath);

    public ShellLayout Load()
    {
        try
        {
            if (!File.Exists(_path))
            {
                return ShellLayout.Empty;
            }

            var json = File.ReadAllText(_path);
            if (string.IsNullOrWhiteSpace(json))
            {
                return ShellLayout.Empty;
            }

            var layout = JsonSerializer.Deserialize<ShellLayout>(json, Options);
            if (layout is null || layout.Version != ShellLayout.Empty.Version)
            {
                // An unknown schema version is discarded rather than guessed at.
                return ShellLayout.Empty;
            }

            return layout;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return ShellLayout.Empty;
        }
    }

    public string? Save(ShellLayout layout)
    {
        ArgumentNullException.ThrowIfNull(layout);

        try
        {
            var directory = System.IO.Path.GetDirectoryName(_path);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }

            // Write-then-replace: a power cut mid-save leaves either the old
            // file or the new one, never a half-written one that Load would
            // have to discard.
            var temporary = _path + ".tmp";
            File.WriteAllText(temporary, JsonSerializer.Serialize(layout, Options));
            File.Move(temporary, _path, overwrite: true);
            return null;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return ex.Message;
        }
    }
}
