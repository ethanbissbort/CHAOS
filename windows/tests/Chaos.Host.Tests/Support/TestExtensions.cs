using System.Text.Json;

namespace Chaos.Host.Tests.Support;

/// <summary>Small helpers shared by the tests.</summary>
internal static class TestExtensions
{
    /// <summary>Reads a response body as a <see cref="JsonElement"/>.</summary>
    /// <param name="response">The response.</param>
    /// <returns>The parsed root element.</returns>
    public static async Task<JsonElement> ReadJsonAsync(this HttpResponseMessage response)
    {
        var body = await response.Content.ReadAsStringAsync();
        Assert.False(string.IsNullOrWhiteSpace(body), "The response body was empty; a gateway must always explain itself.");

        try
        {
            using var document = JsonDocument.Parse(body);
            return document.RootElement.Clone();
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException($"Response body was not JSON: {body}", ex);
        }
    }

    /// <summary>Gets a required string property.</summary>
    /// <param name="element">The object.</param>
    /// <param name="name">Property name.</param>
    /// <returns>The string value.</returns>
    public static string GetStringProperty(this JsonElement element, string name)
    {
        Assert.True(element.TryGetProperty(name, out var property), $"Expected property '{name}' in: {element}");
        return property.GetString() ?? throw new InvalidOperationException($"Property '{name}' was null.");
    }

    /// <summary>
    /// Finds an exception of the given type anywhere in the exception's chain,
    /// including <see cref="AggregateException"/> members.
    /// </summary>
    /// <typeparam name="T">The exception type to find.</typeparam>
    /// <param name="exception">The exception to search.</param>
    /// <returns>The matching exception, or null.</returns>
    public static T? Find<T>(this Exception? exception)
        where T : Exception
    {
        while (exception is not null)
        {
            if (exception is T match)
            {
                return match;
            }

            if (exception is AggregateException aggregate)
            {
                foreach (var inner in aggregate.InnerExceptions)
                {
                    var found = inner.Find<T>();
                    if (found is not null)
                    {
                        return found;
                    }
                }

                return null;
            }

            exception = exception.InnerException;
        }

        return null;
    }

    /// <summary>
    /// Polls until <paramref name="condition"/> holds or the budget expires.
    /// Keeps tests fast without making them timing-fragile.
    /// </summary>
    /// <param name="condition">The condition, evaluated repeatedly.</param>
    /// <param name="description">What is being waited for, used in the failure message.</param>
    /// <param name="timeout">How long to wait. Defaults to 10 seconds.</param>
    /// <returns>A task that completes when the condition holds.</returns>
    public static async Task WaitUntilAsync(Func<Task<bool>> condition, string description, TimeSpan? timeout = null)
    {
        var budget = timeout ?? TimeSpan.FromSeconds(10);
        var deadline = DateTimeOffset.UtcNow + budget;

        while (DateTimeOffset.UtcNow < deadline)
        {
            if (await condition())
            {
                return;
            }

            await Task.Delay(50);
        }

        Assert.Fail($"Timed out after {budget.TotalSeconds:F0}s waiting for: {description}");
    }
}
