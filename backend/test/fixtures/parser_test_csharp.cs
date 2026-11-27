// parser_test_csharp.cs - Comprehensive C# test fixture for CodeKB parser
// Mirrors the Java fixture's intent: covers real-world patterns that the C#
// parser is expected to handle.

using System;
using System.Collections.Generic;

namespace Example.Test
{
    // Delegate: a named callable signature, indexed as a function.
    public delegate bool UserFilter(string username, int minAge);

    // Helper class with static-only methods.
    internal static class StringHelper
    {
        public static string Clean(string data)
        {
            return data != null ? data.Trim() : "";
        }

        private static bool IsEmpty(string str)
        {
            return string.IsNullOrEmpty(str);
        }
    }

    public class UserService
    {
        // Constants
        private const int MAX_USERS = 100;
        public static readonly string DEFAULT_ROLE = "user";

        // Fields
        private string _serviceName;
        protected List<string> Users;

        // Constructor
        public UserService(string serviceName)
        {
            _serviceName = serviceName;
            Users = new List<string>();
        }

        // Public method with multiple calls
        public void AddUser(string username, string email)
        {
            string cleaned = StringHelper.Clean(username);
            if (Users.Count < MAX_USERS && ValidateUser(cleaned))
            {
                Users.Add(cleaned);
                NotifyAdmin(cleaned);
                LogActivity("User added: " + cleaned);
            }
        }

        // Validation method
        private bool ValidateUser(string username)
        {
            return username != null && username.Length > 0;
        }

        // Logging method
        private void LogActivity(string message)
        {
            Console.WriteLine(message);
        }

        // Private method
        private void NotifyAdmin(string username)
        {
            Console.WriteLine("New user added: " + username);
        }

        // Static method
        public static string GetVersion()
        {
            return "1.0.0";
        }

        // Property
        public string ServiceName
        {
            get { return _serviceName; }
        }
    }

    // Interface
    public interface IUserRepository
    {
        void Save(string user);
        string FindById(int id);
    }

    // Enum
    public enum UserRole
    {
        Admin,
        User,
        Guest,
    }

    // Record (C# 9+)
    public record User(string Name, string Email, int Age);

    // Processor class that uses other classes (drives call graph assertions)
    public class UserProcessor
    {
        private UserService userService;

        public UserProcessor()
        {
            userService = new UserService("default");
        }

        public void ProcessUser(string username, string email)
        {
            string cleaned = StringHelper.Clean(username);
            if (cleaned != null && cleaned.Length > 0)
            {
                userService.AddUser(cleaned, email);
            }
        }
    }
}
