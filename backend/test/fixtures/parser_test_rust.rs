// parser_test_rust.rs - Comprehensive Rust test fixture for CodeKB parser
// Mirrors the Go/Java fixtures' intent: covers Rust patterns the parser
// should handle: structs, enums, traits, impl blocks (incl. trait impls),
// associated functions vs methods, modules, constants, and call chains.

#![allow(dead_code)]

use std::collections::HashMap;

// ============================================================================
// CONSTANTS AND STATICS
// ============================================================================

const MAX_USERS: usize = 100;
pub const DEFAULT_ROLE: &str = "user";
static GLOBAL_COUNTER: i32 = 0;

// ============================================================================
// HELPER FUNCTIONS (FREE FUNCTIONS)
// ============================================================================

/// Trims a string and returns the result.
pub fn clean(data: &str) -> String {
    data.trim().to_string()
}

fn is_empty(s: &str) -> bool {
    s.is_empty()
}

// ============================================================================
// STRUCTS
// ============================================================================

pub struct UserService {
    service_name: String,
    users: Vec<String>,
}

impl UserService {
    /// Associated function (no &self) - acts as a constructor.
    pub fn new(service_name: &str) -> Self {
        UserService {
            service_name: service_name.to_string(),
            users: Vec::new(),
        }
    }

    /// Public method that calls multiple other methods + helpers.
    pub fn add_user(&mut self, username: &str, email: &str) {
        let cleaned = clean(username);
        if self.users.len() < MAX_USERS && self.validate_user(&cleaned) {
            self.users.push(cleaned.clone());
            self.notify_admin(&cleaned);
            self.log_activity(&format!("User added: {} ({})", cleaned, email));
        }
    }

    fn validate_user(&self, username: &str) -> bool {
        !is_empty(username)
    }

    fn log_activity(&self, message: &str) {
        println!("{}", message);
    }

    fn notify_admin(&self, username: &str) {
        println!("New user added: {}", username);
    }

    /// Another associated function (no &self).
    pub fn version() -> &'static str {
        "1.0.0"
    }
}

// ============================================================================
// TRAITS AND TRAIT IMPLEMENTATIONS
// ============================================================================

pub trait UserRepository {
    fn save(&mut self, user: &str);
    fn find_by_id(&self, id: u32) -> Option<String>;
}

pub struct InMemoryRepo {
    storage: HashMap<u32, String>,
}

impl InMemoryRepo {
    pub fn new() -> Self {
        InMemoryRepo {
            storage: HashMap::new(),
        }
    }
}

impl UserRepository for InMemoryRepo {
    fn save(&mut self, user: &str) {
        let id = self.storage.len() as u32;
        self.storage.insert(id, user.to_string());
    }

    fn find_by_id(&self, id: u32) -> Option<String> {
        self.storage.get(&id).cloned()
    }
}

// ============================================================================
// ENUMS
// ============================================================================

pub enum UserRole {
    Admin,
    User,
    Guest,
}

// ============================================================================
// PROCESSOR (drives call-graph assertions across types)
// ============================================================================

pub struct UserProcessor {
    service: UserService,
}

impl UserProcessor {
    pub fn new() -> Self {
        UserProcessor {
            service: UserService::new("default"),
        }
    }

    pub fn process_user(&mut self, username: &str, email: &str) {
        let cleaned = clean(username);
        if !is_empty(&cleaned) {
            self.service.add_user(&cleaned, email);
        }
    }
}

// ============================================================================
// NESTED MODULE
// ============================================================================

pub mod inner {
    pub struct Helper;

    impl Helper {
        pub fn assist(&self) -> i32 {
            42
        }
    }
}
