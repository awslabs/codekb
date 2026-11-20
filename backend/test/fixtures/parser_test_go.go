// parser_test_go.go - Comprehensive Go test fixture for CodeKB parser
// This file tests all Phase 1 Go syntax patterns that the parser should handle

package testcode

import (
	"fmt"
	"strings"
)

// ============================================================================
// CONSTANTS AND VARIABLES
// ============================================================================

// Single constant
const MaxRetries = 3

// Constant block
const (
	StatusPending  = "pending"
	StatusActive   = "active"
	StatusComplete = "complete"
)

// Single variable
var GlobalCounter int

// Variable block
var (
	DatabaseURL string
	APIKey      string
	Port        = 8080
)

// ============================================================================
// STRUCTS
// ============================================================================

// User represents a user in the system
type User struct {
	ID        int
	Username  string
	Email     string
	IsActive  bool
	CreatedAt string
}

// Order represents an order with nested struct
type Order struct {
	ID       int
	UserID   int
	Total    float64
	Items    []OrderItem
	Metadata map[string]string
}

// OrderItem is a nested struct
type OrderItem struct {
	ProductID int
	Quantity  int
	Price     float64
}

// Config demonstrates embedded struct
type Config struct {
	User    // embedded struct
	Timeout int
}

// ============================================================================
// INTERFACES
// ============================================================================

// Repository defines data access methods
type Repository interface {
	Get(id int) (interface{}, error)
	Save(item interface{}) error
	Delete(id int) error
}

// Processor defines data processing methods
type Processor interface {
	Process(data []byte) ([]byte, error)
	Validate(data []byte) bool
}

// Combined interface embedding
type DataStore interface {
	Repository
	Processor
}

// ============================================================================
// FUNCTIONS - REGULAR
// ============================================================================

// Add adds two integers
func Add(a, b int) int {
	return a + b
}

// Subtract demonstrates named return values
func Subtract(a, b int) (result int) {
	result = a - b
	return
}

// Divide demonstrates multiple return values with error
func Divide(a, b float64) (float64, error) {
	if b == 0 {
		return 0, fmt.Errorf("division by zero")
	}
	return a / b, nil
}

// ProcessData demonstrates variadic parameters
func ProcessData(items ...string) []string {
	var results []string
	for _, item := range items {
		results = append(results, strings.ToUpper(item))
	}
	return results
}

// Helper is a private function (lowercase)
func helper(data string) string {
	return strings.TrimSpace(data)
}

// ============================================================================
// FUNCTIONS - WITH METHOD RECEIVERS
// ============================================================================

// NewUser is a constructor function
func NewUser(username, email string) *User {
	return &User{
		Username: username,
		Email:    email,
		IsActive: true,
	}
}

// GetFullInfo is a method with value receiver
func (u User) GetFullInfo() string {
	return fmt.Sprintf("%s (%s)", u.Username, u.Email)
}

// Activate is a method with pointer receiver
func (u *User) Activate() {
	u.IsActive = true
}

// Deactivate is a method with pointer receiver
func (u *User) Deactivate() {
	u.IsActive = false
}

// UpdateEmail demonstrates method that calls another method
func (u *User) UpdateEmail(newEmail string) error {
	if newEmail == "" {
		return fmt.Errorf("email cannot be empty")
	}
	u.Email = newEmail
	u.Activate() // method call on receiver
	return nil
}

// Validate is a method that calls regular functions
func (u *User) Validate() bool {
	username := helper(u.Username)
	return len(username) > 0 && u.Email != ""
}

// ============================================================================
// ORDER METHODS
// ============================================================================

// NewOrder creates a new order
func NewOrder(userID int) *Order {
	return &Order{
		UserID: userID,
		Items:  make([]OrderItem, 0),
	}
}

// AddItem adds an item to the order
func (o *Order) AddItem(productID, quantity int, price float64) {
	item := OrderItem{
		ProductID: productID,
		Quantity:  quantity,
		Price:     price,
	}
	o.Items = append(o.Items, item)
	o.calculateTotal() // private method call
}

// calculateTotal is a private method
func (o *Order) calculateTotal() {
	total := 0.0
	for _, item := range o.Items {
		total += float64(item.Quantity) * item.Price
	}
	o.Total = total
}

// GetTotal returns the order total
func (o *Order) GetTotal() float64 {
	return o.Total
}

// ============================================================================
// FUNCTION CALLS - DEMONSTRATING CALL GRAPH
// ============================================================================

// ProcessOrder demonstrates a function that calls multiple other functions
func ProcessOrder(userID int, items []OrderItem) (*Order, error) {
	// Create order
	order := NewOrder(userID)

	// Add items
	for _, item := range items {
		order.AddItem(item.ProductID, item.Quantity, item.Price)
	}

	// Validate
	if order.GetTotal() <= 0 {
		return nil, fmt.Errorf("order total must be positive")
	}

	return order, nil
}

// ValidateUser demonstrates calling methods and functions
func ValidateUser(u *User) error {
	if !u.Validate() {
		return fmt.Errorf("user validation failed")
	}

	info := u.GetFullInfo()
	processed := helper(info)

	if len(processed) == 0 {
		return fmt.Errorf("processed info is empty")
	}

	return nil
}

// BatchProcess demonstrates nested function calls
func BatchProcess(users []*User) []string {
	results := make([]string, 0)

	for _, user := range users {
		if err := ValidateUser(user); err != nil {
			continue
		}
		results = append(results, user.GetFullInfo())
	}

	return ProcessData(results...)
}

// ============================================================================
// INTERFACE IMPLEMENTATIONS (IMPLICIT)
// ============================================================================

// UserRepository implements Repository interface
type UserRepository struct {
	users map[int]*User
}

// NewUserRepository creates a new user repository
func NewUserRepository() *UserRepository {
	return &UserRepository{
		users: make(map[int]*User),
	}
}

// Get retrieves a user by ID (implements Repository.Get)
func (r *UserRepository) Get(id int) (interface{}, error) {
	user, ok := r.users[id]
	if !ok {
		return nil, fmt.Errorf("user not found")
	}
	return user, nil
}

// Save stores a user (implements Repository.Save)
func (r *UserRepository) Save(item interface{}) error {
	user, ok := item.(*User)
	if !ok {
		return fmt.Errorf("invalid item type")
	}
	r.users[user.ID] = user
	return nil
}

// Delete removes a user (implements Repository.Delete)
func (r *UserRepository) Delete(id int) error {
	delete(r.users, id)
	return nil
}

// ============================================================================
// COMPLEX CALL CHAINS
// ============================================================================

// Service demonstrates a service layer
type Service struct {
	repo Repository
}

// NewService creates a new service
func NewService(repo Repository) *Service {
	return &Service{repo: repo}
}

// CreateUser demonstrates a multi-step process with multiple calls
func (s *Service) CreateUser(username, email string) (*User, error) {
	// Create user
	user := NewUser(username, email)

	// Validate
	if err := ValidateUser(user); err != nil {
		return nil, err
	}

	// Save
	if err := s.repo.Save(user); err != nil {
		return nil, err
	}

	return user, nil
}

// GetUser retrieves and validates a user
func (s *Service) GetUser(id int) (*User, error) {
	// Retrieve from repo
	item, err := s.repo.Get(id)
	if err != nil {
		return nil, err
	}

	// Type assertion
	user, ok := item.(*User)
	if !ok {
		return nil, fmt.Errorf("invalid user type")
	}

	// Validate
	if err := ValidateUser(user); err != nil {
		return nil, err
	}

	return user, nil
}

// ============================================================================
// CLOSURES AND ANONYMOUS FUNCTIONS
// ============================================================================

// MapUsers demonstrates using closures
func MapUsers(users []*User, transform func(*User) string) []string {
	results := make([]string, len(users))
	for i, user := range users {
		results[i] = transform(user)
	}
	return results
}

// FilterUsers demonstrates filter with closure
func FilterUsers(users []*User, predicate func(*User) bool) []*User {
	var filtered []*User
	for _, user := range users {
		if predicate(user) {
			filtered = append(filtered, user)
		}
	}
	return filtered
}

// GetActiveUsers uses closures in action
func GetActiveUsers(users []*User) []string {
	// Filter active users
	active := FilterUsers(users, func(u *User) bool {
		return u.IsActive
	})

	// Map to info strings
	return MapUsers(active, func(u *User) string {
		return u.GetFullInfo()
	})
}

// ============================================================================
// TYPE ALIASES AND CUSTOM TYPES
// ============================================================================

// UserID is a type alias
type UserID int

// Status is a custom string type
type Status string

// HandlerFunc is a function type
type HandlerFunc func(data []byte) error

// Pipeline demonstrates using function types
type Pipeline struct {
	handlers []HandlerFunc
}

// NewPipeline creates a new pipeline
func NewPipeline() *Pipeline {
	return &Pipeline{
		handlers: make([]HandlerFunc, 0),
	}
}

// Add adds a handler to the pipeline
func (p *Pipeline) Add(handler HandlerFunc) {
	p.handlers = append(p.handlers, handler)
}

// Execute runs all handlers
func (p *Pipeline) Execute(data []byte) error {
	for _, handler := range p.handlers {
		if err := handler(data); err != nil {
			return err
		}
	}
	return nil
}
