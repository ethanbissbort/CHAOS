namespace Chaos.Host.Web;

/// <summary>
/// Where the operator console assets were found, or where the gateway looked and failed.
/// </summary>
/// <param name="Path">The resolved absolute directory, or null when nothing was found.</param>
/// <param name="Source">How it was resolved: <c>configured</c>, <c>content-root</c>, <c>app-directory</c>, <c>repository</c>, or <c>not-found</c>.</param>
/// <param name="SearchedPaths">Every candidate that was probed, in order. Reported on <c>/host/info</c> so a missing UI is diagnosable without guessing.</param>
public sealed record WebRootResolution(string? Path, string Source, IReadOnlyList<string> SearchedPaths)
{
    /// <summary>Whether a directory was found.</summary>
    public bool Present => Path is not null;
}
