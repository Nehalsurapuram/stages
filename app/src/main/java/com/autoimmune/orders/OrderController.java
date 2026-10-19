package com.autoimmune.orders;

import java.math.BigDecimal;
import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService service;

    public OrderController(OrderService service) {
        this.service = service;
    }

    @GetMapping
    public List<Order> list() {
        return service.findAll();
    }

    @GetMapping("/{id}/total")
    public BigDecimal total(@PathVariable long id) {
        return service.total(id);
    }

    @GetMapping("/{id}/discount")
    public BigDecimal discount(@PathVariable long id) {
        return service.discountedTotal(id);
    }

    @GetMapping("/search")
    public List<Order> search(@RequestParam(required = false) String customer) {
        return service.searchByCustomer(customer);
    }
}
