package com.example.test;

import java.util.List;
import java.util.ArrayList;

/**
 * Parser test fixture - Java
 * Comprehensive test code covering real-world patterns
 */

// Helper class
class StringHelper {
    public static String clean(String data) {
        return data != null ? data.trim() : "";
    }
    
    private static boolean isEmpty(String str) {
        return str == null || str.isEmpty();
    }
}

public class UserService {
    
    // Constants
    private static final int MAX_USERS = 100;
    public static final String DEFAULT_ROLE = "user";
    
    // Fields
    private String serviceName;
    protected List<String> users;
    
    // Constructor
    public UserService(String serviceName) {
        this.serviceName = serviceName;
        this.users = new ArrayList<>();
    }
    
    // Public method with multiple calls
    public void addUser(String username, String email) {
        String cleaned = StringHelper.clean(username);
        if (users.size() < MAX_USERS && validateUser(cleaned)) {
            users.add(cleaned);
            notifyAdmin(cleaned);
            logActivity("User added: " + cleaned);
        }
    }
    
    // Validation method
    private boolean validateUser(String username) {
        return username != null && !username.isEmpty();
    }
    
    // Logging method
    private void logActivity(String message) {
        System.out.println(message);
    }
    
    // Private method
    private void notifyAdmin(String username) {
        System.out.println("New user added: " + username);
    }
    
    // Static method
    public static String getVersion() {
        return "1.0.0";
    }
    
    // Abstract method example (in abstract class)
    // public abstract void processUser(String username);
    
    // Nested class
    public static class UserValidator {
        public boolean validate(String username) {
            return username != null && !username.isEmpty();
        }
    }
}

// Interface
interface UserRepository {
    void save(String user);
    String findById(int id);
}

// Enum
enum UserRole {
    ADMIN,
    USER,
    GUEST
}

// Record (Java 14+)
record User(String name, String email, int age) {
    // Compact constructor
    public User {
        if (age < 0) {
            throw new IllegalArgumentException("Age cannot be negative");
        }
    }
}

// Processor class that uses other classes
class UserProcessor {
    private UserService userService;
    
    public UserProcessor() {
        this.userService = new UserService("default");
    }
    
    public void processUser(String username, String email) {
        String cleaned = StringHelper.clean(username);
        if (cleaned != null && !cleaned.isEmpty()) {
            userService.addUser(cleaned, email);
        }
    }
}
